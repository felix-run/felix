"""A code-defined tool's own `prompt_guidance` survives the governance stack.

Every tool is cloned by each wrapper on its way through the compile; a field the clone forgot
would be reset on every governed tool, and the guidance would silently vanish from the prompt.
"""

from __future__ import annotations

from typing import Any

from felix.limits import effective_limits
from felix.manifests.builder import apply_limits, tool_guidance_section
from felix.manifests.schema import Limits
from felix.tools.types import define_tool


async def _noop(_a: Any = None, _c: Any = None) -> str:
    return ""


def test_a_tools_own_guidance_survives_a_wrapper_and_merges_with_the_manifests() -> None:
    tool = define_tool(
        name="lookup", description="look up", handler=_noop, prompt_guidance="Look before guessing."
    )
    [wrapped] = apply_limits([tool], effective_limits(Limits()), "m")
    assert wrapped is not tool, "the wrapper cloned it"
    section = tool_guidance_section(
        [wrapped], {"look*": "Cite what `lookup` returned.", "absent": "never shown"}
    )
    assert section == "Tool guidance:\n- Look before guessing.\n- Cite what `lookup` returned."
