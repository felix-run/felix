"""One parser for the Compose files, shared by every overlay test.

`ruamel.yaml` rather than `pyyaml`: it is what the harness declares and uses, so it is
present by contract. `yaml` is importable here only as somebody else's transitive
dependency (pre-commit, presidio), which is a thing that stops being true without warning.

Compose's `!reset` and `!override` merge tags are not YAML, so a plain safe loader refuses
any file that uses them — which is every overlay that drops a `build:` or a port list.
The tags are kept as their plain value here: `!reset null` reads as `None`, `!override []`
as `[]`. `${VAR:?err}` interpolation is Compose's too, so those stay raw strings, which is
all a structural assertion needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.nodes import MappingNode, SequenceNode


class _ComposeConstructor(SafeConstructor):
    """Subclassed so the tag handlers do not land on ruamel's shared SafeConstructor."""


def _tagged(constructor: SafeConstructor, suffix: str, node: Any) -> Any:
    if isinstance(node, SequenceNode):
        return constructor.construct_sequence(node, deep=True)
    if isinstance(node, MappingNode):
        return constructor.construct_mapping(node, deep=True)
    value = constructor.construct_scalar(node)
    return None if value in ("", "null", "~") else value


_ComposeConstructor.add_multi_constructor("!", _tagged)


def load_compose(path: Path) -> dict[str, Any]:
    """Parse one Compose file (no overlay merging — assert on the file as written)."""
    yaml = YAML(typ="safe")
    yaml.Constructor = _ComposeConstructor
    return yaml.load(path.read_text(encoding="utf-8")) or {}


__all__ = ["load_compose"]
