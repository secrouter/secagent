"""Review persona / alignment loading.

A persona is plain YAML (see ``config/alignment/*.yaml``). It is loaded fresh on each
review so edits take effect without a restart — the "simple way to edit the agent's
alignment and verbosity". Unknown keys are ignored, so profiles can evolve safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ...config import resolve_config_path

_VERBOSITY_DIRECTIVES = {
    "terse": "Be very concise. Raise only the most important points. Prefer bullet lists.",
    "normal": "Be concise but complete. Group related points. Avoid repetition.",
    "detailed": "Be thorough. Explain reasoning and cite specifics, but stay on-point.",
}


@dataclass
class Persona:
    name: str = "default"
    alignment: str = "You are a constructive senior engineer reviewing a merge request."
    verbosity: str = "normal"
    focus_areas: list[str] = field(default_factory=lambda: ["correctness", "security"])
    tone: list[str] = field(default_factory=lambda: ["professional"])
    max_inline_comments: int = 8
    max_reply_paragraphs: int = 3
    use_affordances: bool = True

    def verbosity_directive(self) -> str:
        return _VERBOSITY_DIRECTIVES.get(self.verbosity, _VERBOSITY_DIRECTIVES["normal"])

    def system_prompt(self) -> str:
        focus = ", ".join(self.focus_areas) or "correctness"
        tone = ", ".join(self.tone) or "professional"
        return (
            f"{self.alignment.strip()}\n\n"
            f"Focus areas, in priority order: {focus}.\n"
            f"Tone: {tone}.\n"
            f"{self.verbosity_directive()}\n"
            f"Raise at most {self.max_inline_comments} distinct points. "
            "Always reference file paths and line context. If you lack information, say "
            "so rather than guessing. Format your output as Markdown suitable for a "
            "GitLab merge-request comment."
        )


def load_persona(profile_path: str | Path) -> Persona:
    """Load a persona from YAML, falling back to defaults for any missing keys."""
    p = resolve_config_path(profile_path)
    if not p.exists():
        return Persona()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    limits = data.get("limits", {}) or {}
    return Persona(
        name=data.get("name", "default"),
        alignment=data.get("alignment", Persona.alignment),
        verbosity=str(data.get("verbosity", "normal")).lower(),
        focus_areas=list(data.get("focus_areas", []) or Persona().focus_areas),
        tone=list(data.get("tone", []) or Persona().tone),
        max_inline_comments=int(limits.get("max_inline_comments", 8)),
        max_reply_paragraphs=int(limits.get("max_reply_paragraphs", 3)),
        use_affordances=bool(data.get("use_affordances", True)),
    )
