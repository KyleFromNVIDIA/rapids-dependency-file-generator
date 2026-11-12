"""Modify dependency lists in Rattler recipes while preserving surrounding text."""

import os
import typing

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ._config import FileExtras
from ._constants import cli_name


def _member(node: Node, key: str) -> tuple[ScalarNode, Node]:
    if not isinstance(node, MappingNode):
        raise ValueError(f"Recipe path component '{key}' must be inside a mapping.")
    matches = [(k, v) for k, v in node.value if isinstance(k, ScalarNode) and k.value == key]
    if len(matches) != 1:
        raise ValueError(f"Recipe path component '{key}' must exist exactly once.")
    return matches[0]


def _target(root: Node, extras: FileExtras) -> tuple[ScalarNode, SequenceNode]:
    node = root
    if extras.output_type in {"package", "staging"}:
        kind = extras.output_type
        name = extras.package_name if kind == "package" else extras.staging_name
        outputs = _member(root, "outputs")[1]
        matches = []
        if isinstance(outputs, SequenceNode):
            for output in outputs.value:
                # Conditional outputs are deliberately not traversed.
                if not isinstance(output, MappingNode):
                    continue
                names = [v for k, v in output.value if k.value == kind]
                for section in names:
                    if isinstance(section, MappingNode):
                        if any(
                            k.value == "name" and isinstance(v, ScalarNode) and v.value == name
                            for k, v in section.value
                        ):
                            matches.append(output)
        if len(matches) != 1:
            raise ValueError(f"Recipe {kind} name '{name}' must match exactly one output.")
        node = matches[0]
    elif extras.output_type != "requirements":
        raise ValueError("Recipe 'output_type' must be requirements, package, or staging.")

    if extras.test_index is not None:
        if extras.output_type == "staging" or extras.requirements_type not in {"build", "run"}:
            raise ValueError("Recipe test requirements must be build or run and cannot target staging outputs.")
        tests = _member(node, "tests")[1]
        if not isinstance(tests, SequenceNode) or extras.test_index < 0 or extras.test_index >= len(tests.value):
            raise ValueError(f"Recipe test_index {extras.test_index} does not reference an existing test.")
        node = tests.value[extras.test_index]

    node = _member(node, "requirements")[1]
    if not extras.requirements_type:
        raise ValueError("Recipe 'requirements_type' must be specified.")
    for component in extras.requirements_type.split("."):
        parent = node
        key, node = _member(parent, component)
    if isinstance(parent, MappingNode) and parent.flow_style:
        raise ValueError("The recipe dependency list must belong to a block-style mapping.")
    if not isinstance(node, SequenceNode) or (node.flow_style and node.value):
        raise ValueError("The recipe dependency list must be a block list or an empty inline list ([]).")
    return key, node


class _RecipeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def update_recipe(
    contents: str,
    dependencies: typing.Sequence[typing.Union[str, dict[str, typing.Any]]],
    extras: FileExtras,
    relative_config: str,
) -> str:
    """Replace one existing dependency list using YAML source marks."""
    root = yaml.compose(contents)
    if root is None:
        raise ValueError("The recipe must contain a YAML mapping.")
    key, sequence = _target(root, extras)
    colon = key.end_mark.index
    while contents[colon : colon + 1] in {" ", "\t"}:
        colon += 1
    if contents[colon : colon + 1] != ":" or sequence.start_mark.index <= colon:
        raise ValueError("Recipe dependency lists cannot be aliases or use explicit mapping keys.")

    # Block end marks can include the indentation and comments preceding the next
    # key. Use the last actual token to keep those surrounding bytes intact.
    tokens = [
        t
        for t in yaml.scan(contents)
        if sequence.start_mark.index <= t.start_mark.index < sequence.end_mark.index
        and t.end_mark.index > t.start_mark.index
    ]
    if not tokens:
        raise ValueError("Cannot locate the recipe dependency list in the source.")
    last = tokens[-1].end_mark
    end = last.index if last.column == 0 else contents.find("\n", last.index)
    if end == -1:
        end = len(contents)
    elif last.column != 0:
        end += 1

    newline = "\r\n" if "\r\n" in contents else "\n"
    line_end = contents.find("\n", colon)
    if line_end == -1:
        line_end = len(contents)
    metadata_tokens = [
        t
        for t in tokens
        if isinstance(t, (yaml.tokens.AnchorToken, yaml.tokens.TagToken))
        and t.start_mark.index == sequence.start_mark.index
    ]
    metadata_end = metadata_tokens[-1].end_mark.index if metadata_tokens else sequence.start_mark.index
    value_prefix = " " + contents[sequence.start_mark.index : metadata_end] if metadata_tokens else ""
    list_line_start = contents.rfind("\n", 0, sequence.start_mark.index) + 1
    preceding = contents[line_end + 1 : list_line_start]
    if sequence.flow_style and sequence.start_mark.line == key.end_mark.line:
        prefix = contents[sequence.end_mark.index : line_end].rstrip("\r")
    else:
        prefix_start = metadata_end if metadata_tokens else colon + 1
        prefix = contents[prefix_start:line_end].rstrip("\r")
        if sequence.flow_style:
            value_line_end = contents.find("\n", sequence.end_mark.index)
            if value_line_end == -1:
                value_line_end = len(contents)
            value_comment = contents[sequence.end_mark.index : value_line_end].strip()
            if value_comment:
                preceding += " " * (key.start_mark.column + 2) + value_comment + newline

    marker = f"This list was generated by `{cli_name}`."
    # A comment preceding a block list stays in place when that list becomes
    # empty. On later edits it follows the inline [], so include it again.
    if sequence.flow_style:
        trailing = ""
        for line in contents[end:].splitlines(keepends=True):
            if not line.strip() or (
                line.lstrip().startswith("#") and len(line) - len(line.lstrip()) > key.start_mark.column
            ):
                trailing += line
            else:
                break
        if marker in trailing:
            preceding += trailing
            end += len(trailing)

    # Also recognize a generation comment immediately above the containing key.
    above = []
    key_line_start = contents.rfind("\n", 0, key.start_mark.index) + 1
    for line in reversed(contents[:key_line_start].splitlines()):
        if line.lstrip().startswith("#"):
            above.append(line)
        else:
            break
    notice = f"This list was generated by `{cli_name}`. To make changes, edit {relative_config} and run `{cli_name}`."
    indent = " " * (key.start_mark.column + 2)
    if marker not in prefix + preceding + "\n".join(above):
        if prefix.strip():
            prefix += " " + notice
        else:
            prefix = "  # " + notice

    if dependencies:
        rendered = yaml.dump(list(dependencies), Dumper=_RecipeDumper, sort_keys=False, width=float("inf"))
        rendered = "".join(indent + line if line.strip() else line for line in rendered.splitlines(keepends=True))
        rendered = rendered.replace("\n", newline)
        replacement = value_prefix + prefix + newline + preceding + rendered
    else:
        replacement = value_prefix + " []" + prefix + newline + preceding
    # A file without a final newline keeps that property.
    if end == len(contents) and not contents.endswith("\n"):
        replacement = replacement.rstrip("\n")
    return contents[: colon + 1] + replacement + contents[end:]


def read_recipe(output_dir: os.PathLike, file_name: str) -> str:
    """Read an existing recipe without normalizing its line endings."""
    with open(os.path.join(output_dir, file_name), newline="") as f:
        return f.read()
