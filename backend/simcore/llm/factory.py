from __future__ import annotations

from typing import Any

from simcore.config import resolve_role_settings
from simcore.llm.base import LLMProvider
from simcore.llm.cli_agent import CLI_KINDS, CliAgentProvider
from simcore.llm.mock import MockProvider
from simcore.llm.openai_compat import OpenAICompatProvider


def create_provider(settings: dict[str, Any], role: str, scenario: dict[str, Any] | None = None) -> LLMProvider:
    config = resolve_role_settings(settings, role)
    if config["provider"] == "mock":
        return MockProvider(scenario)
    if config["provider"] in CLI_KINDS:
        # 本机已登录的 Claude Code / Codex CLI：不需要 API key，模型跟随 CLI 当前设置（model 为 default 时）
        return CliAgentProvider(CLI_KINDS[config["provider"]], str(config["model"]))
    return OpenAICompatProvider(
        str(config["base_url"]),
        str(config["api_key"]),
        str(config["model"]),
        extra_headers=config["extra_headers"],
        params_override=config["params_override"],
    )
