"""Assemble the actor prompt from the skill template."""

from __future__ import annotations

from importlib import resources

from ..memory.base import MemoryAdapter


def load_template() -> str:
    text = resources.files("ouroboros.skills").joinpath("ouroboros-actor/SKILL.md").read_text()
    # strip front matter
    if text.startswith("---"):
        _, _, rest = text.partition("\n---\n")
        text = rest.lstrip("\n")
    return text


def build_actor_prompt(
    *,
    goal_text: str,
    memory: MemoryAdapter,
    iteration: int,
    injected: str | None = None,
    template: str | None = None,
) -> str:
    tpl = template or load_template()
    return (
        tpl.replace("{iteration}", str(iteration))
        .replace("{goal}", goal_text.strip() or "(no goal.md yet — run `ouroboros init`)")
        .replace("{orient}", memory.orient_prompt())
        .replace("{injected}", injected or "(none)")
        .replace("{record}", memory.record_prompt())
    )
