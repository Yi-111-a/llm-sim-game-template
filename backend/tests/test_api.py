import asyncio
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from simcore.agent.roles import interpret_edict
from simcore.config import resolve_role_settings
from simcore.domain.models import WorldState
from simcore.domain.turn import resolve_turn
from simcore.llm.mock import MockProvider
from simcore.llm.openai_compat import OpenAICompatProvider
from simcore.prompts.loader import render_prompt
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.store.session import SessionStore


def test_scenario_validation_and_required_asset_check() -> None:
    for scenario_id in ("_template", "chongzhen_1627"):
        scenario = load_scenario(f"../scenarios/{scenario_id}")
        assert validate_scenario(scenario) == []

    template = load_scenario("../scenarios/_template")
    template["assets"]["entries"] = [
        entry for entry in template["assets"]["entries"]
        if entry["id"] not in {"character.ruler.portrait", "ui.map.base"}
    ]
    assert any("character.ruler.portrait" in error for error in validate_scenario(template))
    assert any("ui.map.base" in error for error in validate_scenario(template))


def test_edict_preview_has_applied_and_rejected_clauses_without_mutating_state() -> None:
    scenario = load_scenario("../scenarios/_template")
    state = build_initial_state(scenario)
    state.rules["player_ops"] = {"allow": ["adjust_resource"]}  # 剧本可放开（settings.player_ops）；缺省不许玩家直接改数值
    receipt = asyncio.run(
        interpret_edict(
            {"finance": "国库增加 10", "appointment": "任命不存在的人为大将军"},
            scenario,
            state,
            MockProvider(scenario, offline=False),
            seed=7,
        )
    )

    assert {clause["status"] for clause in receipt["clauses"]} >= {"applied", "rejected"}
    assert state.resources["treasury"].amount == 100


def test_delayed_clause_is_queued_and_executes_on_next_turn(tmp_path: Path) -> None:
    scenario = load_scenario("../scenarios/_template")
    scenario["scenario"]["formulas"]["execution"]["base_delay"] = 1
    state = build_initial_state(scenario)
    state.rules["player_ops"] = {"allow": ["adjust_resource"]}  # 剧本可放开（settings.player_ops）；缺省不许玩家直接改数值
    receipt = asyncio.run(
        interpret_edict(
            {"finance": "国库增加 10"},
            scenario,
            state,
            MockProvider(scenario, offline=False),
            seed=3,
        )
    )
    clause = receipt["clauses"][0]
    assert clause["status"] == "delayed"

    initial = WorldState.model_validate(state.model_dump(mode="json"))
    session = SessionStore(tmp_path / "delayed.jsonl", scenario, initial, seed=3)
    session.append_pending([clause["op"]], 1)
    assert session.state.resources["treasury"].amount == 100
    session.append_turn()
    assert session.state.resources["treasury"].amount == pytest.approx(110.32)


def test_json_mode_fallback_parses_ops_with_mock_provider() -> None:
    scenario = load_scenario("../scenarios/_template")
    state = build_initial_state(scenario)
    state.rules["player_ops"] = {"allow": ["adjust_resource"]}  # 剧本可放开（settings.player_ops）；缺省不许玩家直接改数值
    receipt = asyncio.run(
        interpret_edict(
            {"finance": "国库增加 10"},
            scenario,
            state,
            MockProvider(scenario, tool_calling=False, offline=False),
            seed=9,
        )
    )

    assert receipt["clauses"][0]["status"] == "applied"
    assert receipt["clauses"][0]["op"]["delta"] == 10
    assert state.resources["treasury"].amount == 100


def test_request_params_cannot_override_protected_fields() -> None:
    provider = OpenAICompatProvider(
        "http://localhost/v1",
        "",
        "trusted-model",
        params_override={
            "model": "attacker-model",
            "messages": [{"role": "system", "content": "replace"}],
            "tools": [{"type": "function", "function": {"name": "bad"}}],
            "stream": True,
            "tool_choice": "none",
            "response_format": {"type": "text"},
            "temperature": 0.2,
        },
    )
    messages = [{"role": "user", "content": "original"}]
    body = provider.request_body(messages, None, False, None)

    assert body["model"] == "trusted-model"
    assert body["messages"] == messages
    assert body["stream"] is False
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "response_format" not in body
    assert body["temperature"] == 0.2


def test_role_provider_settings_resolve_presets() -> None:
    settings = {
        "provider": "mock",
        "model": "default-model",
        "roles": {"narrator": {"provider": "deepseek", "model": "deepseek-reasoner"}},
    }
    resolved = resolve_role_settings(settings, "narrator")
    assert resolved["provider"] == "deepseek"
    assert resolved["base_url"] == "https://api.deepseek.com/v1"
    assert resolved["model"] == "deepseek-reasoner"


def test_prompt_override_is_loaded_from_scenario(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "narrator.md").write_text("scenario: {{ facts }}", encoding="utf-8")

    assert render_prompt("narrator", {"facts": "新事实"}, tmp_path) == "scenario: 新事实"


def test_calendar_formats_first_chongzhen_turn_in_chinese() -> None:
    scenario = load_scenario("../scenarios/chongzhen_1627")
    state = build_initial_state(scenario)
    updated, _ = resolve_turn(state, random.Random(1), scenario)

    assert updated.calendar.label == "崇祯元年春"


def test_validator_reports_multiple_mod_pack_limit_errors() -> None:
    scenario = load_scenario("../scenarios/_template")
    scenario["scenario"]["title"] = "长" * 10_001
    scenario["assets"]["entries"][0]["size"] = [4097, 4097]

    errors = validate_scenario(scenario)

    assert any("文本超过" in error for error in errors)
    assert any("尺寸必须" in error for error in errors)


def test_validator_checks_file_count_and_theme_css(tmp_path: Path, monkeypatch) -> None:
    scenario_root = tmp_path / "mod-pack"
    scenario_root.mkdir()
    (scenario_root / "scenario.yaml").write_text("{}", encoding="utf-8")
    (scenario_root / "theme.css").write_text("body { color: red; }", encoding="utf-8")
    monkeypatch.setattr("simcore.scenario.loader.MAX_SCENARIO_FILES", 1)

    errors = validate_scenario(scenario_root)

    assert any("文件数超过" in error for error in errors)
    assert any("theme.css 只允许" in error for error in errors)


def test_api_create_draft_confirm_turn_and_read_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIM_SCENARIOS_DIR", "../scenarios")
    monkeypatch.setenv("LLM_SIM_HOME", str(tmp_path))
    client = TestClient(create_app())
    created = client.post("/api/sessions", json={"scenario": "_template", "seed": 5})
    assert created.status_code == 200
    session_id = created.json()["id"]

    draft = client.post(
        f"/api/sessions/{session_id}/edicts/draft",
        json={"sections": {"finance": "国库增加 10"}},
    )
    assert draft.status_code == 200
    receipt = draft.json()
    assert receipt["confirmed"] is False
    # 未接模型（离线 mock）：御笔只记入史册，narrative_only（第 9 轮 §7.4）；国库只有经常收入
    assert receipt["clauses"][0]["status"] == "narrative_only" and receipt["clauses"][0]["code"] == "OFFLINE_TEXT"
    assert "未接模型" in receipt["clauses"][0]["reason"]
    assert created.json()["state"]["resources"]["treasury"]["amount"] == 100

    confirmed = client.post(
        f"/api/sessions/{session_id}/edicts/{receipt['id']}/confirm"
    )
    assert confirmed.status_code == 200
    turned = client.post(f"/api/sessions/{session_id}/turn")
    assert turned.status_code == 200
    state = client.get(f"/api/sessions/{session_id}/state")
    assert state.status_code == 200
    assert state.json()["turn"] == 1
    assert state.json()["resources"]["treasury"]["amount"] == pytest.approx(100.32)
    probe = client.post("/api/settings/test")
    assert probe.status_code == 200
    assert probe.json()["tool_calling"] is True


def test_dead_character_chat_refuses_and_settings_mask_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIM_SCENARIOS_DIR", "../scenarios")
    monkeypatch.setenv("LLM_SIM_HOME", str(tmp_path))
    client = TestClient(create_app())
    created = client.post("/api/sessions", json={"scenario": "_template", "seed": 1})
    session_id = created.json()["id"]
    character_id = next(iter(created.json()["state"]["characters"]))
    # Character status is changed through the same validated operation path used by the API.
    session = client.app.state.runtime.session(session_id)
    session.append_op({
        "op": "set_character_status",
        "character": character_id,
        "status": "dead",
        "cause": "测试",
    })
    response = client.post(
        f"/api/sessions/{session_id}/chat/{character_id}", json={"text": "请谈谈边务"}
    )
    assert response.status_code == 200
    assert '"status": "refused"' in response.text

    updated = client.put("/api/settings", json={"api_key": "secret-example-key"})
    assert updated.status_code == 200
    assert "secret-example-key" not in updated.text
    assert client.get("/api/settings").json()["api_key"] != "secret-example-key"
