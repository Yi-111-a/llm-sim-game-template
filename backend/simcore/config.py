from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

ROLE_NAMES = (
    "edict_interpreter",
    "narrator",
    "character_chat",
    "court_debate",
    "edict_polish",
    "chronicler",
)
PROTECTED_PARAMS = {"messages", "model", "tools", "stream", "tool_choice", "response_format"}

def settings_path() -> Path:
    return Path(os.environ.get("LLM_SIM_HOME", Path.home() / ".llm-sim")) / "settings.json"


def load_settings() -> dict[str, Any]:
    path = settings_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                return stored
        except json.JSONDecodeError:
            pass
    return {
        "provider": os.environ.get("SIM_PROVIDER", "mock"),
        "base_url": os.environ.get("SIM_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("SIM_MODEL", "gpt-4o-mini"),
        "api_key": os.environ.get("SIM_API_KEY", ""),
        "image_provider": os.environ.get("SIM_IMAGE_PROVIDER", "mock"),
        "image_base_url": os.environ.get("SIM_IMAGE_BASE_URL", "https://api.openai.com/v1"),
        "image_model": os.environ.get("SIM_IMAGE_MODEL", "gpt-image-1"),
        "image_api_key": os.environ.get("SIM_IMAGE_API_KEY", ""),
        "roles": {role: {"provider": "default", "model": "default"} for role in ROLE_NAMES},
    }


def provider_presets() -> dict[str, Any]:
    path = Path(__file__).parent / "llm" / "presets.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    providers = data.get("providers", {}) if isinstance(data, dict) else {}
    return providers if isinstance(providers, dict) else {}


def resolve_role_settings(settings: dict[str, Any], role: str) -> dict[str, Any]:
    roles = settings.get("roles", {})
    role_config = roles.get(role, {}) if isinstance(roles, dict) else {}
    selected = str(role_config.get("provider", "default"))
    using_default = selected == "default"
    provider_name = str(settings.get("provider", "mock")) if using_default else selected
    preset = provider_presets().get(provider_name, {})
    provider_configs = settings.get("provider_configs", {})
    custom_config = provider_configs.get(provider_name, {}) if isinstance(provider_configs, dict) else {}
    preset = {**preset, **custom_config}

    global_model = str(settings.get("model", preset.get("model", "gpt-4o-mini")))
    model_choice = str(role_config.get("model", "default"))
    model = global_model if model_choice == "default" and using_default else (
        str(preset.get("model", global_model)) if model_choice == "default" else model_choice
    )
    if using_default:
        base_url = str(settings.get("base_url", preset.get("base_url", "")))
        api_key = str(settings.get("api_key", ""))
    else:
        base_url = str(role_config.get("base_url", preset.get("base_url", "")))
        api_key = str(role_config.get("api_key", ""))
    api_key_env = str(preset.get("api_key_env", ""))
    if not api_key:
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    headers = dict(preset.get("extra_headers", {}))
    headers.update(role_config.get("extra_headers", {}))
    params = dict(preset.get("params_override", {}))
    params.update(settings.get("params_override", {}))
    params.update(role_config.get("params_override", {}))
    return {
        "provider": provider_name,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "extra_headers": headers,
        "params_override": params,
        "api_key_url": preset.get("api_key_url", ""),
    }


def save_settings(settings: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return _mask_secrets(settings)


def _mask_secrets(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: _mask_secrets(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_mask_secrets(item, key) for item in value]
    if key.endswith("_api_key") or key == "api_key":
        secret = str(value or "")
        return f"{secret[:3]}…{secret[-2:]}" if len(secret) > 6 else ("已设置" if secret else "")
    return value
