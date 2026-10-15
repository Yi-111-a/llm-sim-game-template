from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Template


def render_prompt(
    name: str, values: dict[str, Any], scenario_root: str | Path | None = None
) -> str:
    path = Path(__file__).parent / f"{name}.md"
    if scenario_root is not None:
        prompts_root = (Path(scenario_root) / "prompts").resolve()
        override = (prompts_root / f"{Path(name).name}.md").resolve()
        if prompts_root in override.parents and override.is_file():
            path = override
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---\n"):
        _, _, raw = raw.partition("---\n")
        _, _, raw = raw.partition("---\n")
    return Template(raw).render(**values).strip()
