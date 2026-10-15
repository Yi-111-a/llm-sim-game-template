from __future__ import annotations

from typing import Any

from simcore.config import resolve_role_settings
from simcore.llm.base import LLMProvider
from simcore.llm.mock import MockProvider
from simcore.llm.openai_compat import OpenAICompatProvider


def create_provider(settings: dict[str, Any], role: str, scenario: dict[str, Any] | None = None) -> LLMProvider:
    config = resolve_role_settings(settings, role)
    if config["provider"] == "mock":
        return MockProvider(scenario)
    return OpenAICompatProvider(
        str(config["base_url"]),
        str(config["api_key"]),
        str(config["model"]),
        extra_headers=config["extra_headers"],
        params_override=config["params_override"],
    )
