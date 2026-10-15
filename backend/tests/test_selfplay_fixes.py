"""《天宝》自玩发现的问题的回归测试：草案持久化、延期按组、玩家本人不可召对、事实文本自足、数字格式、头衔、性能缓存。"""

import asyncio
import os
import random
import re
import sys
from pathlib import Path

import pytest
from conftest import FIXTURE_SCENARIOS
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters

from simcore.agent.roles import interpret_edict
from simcore.domain import turn as turn_module
from simcore.domain.apply import apply
from simcore.domain.models import TurnReport, WorldState, clone
from simcore.domain.rules import player_character_id
from simcore.domain.visibility import format_number, public_report, public_view, redacted_report
from simcore.scenario.loader import build_initial_state, load_scenario
from simcore.service import GameError, GameService
from simcore.store.session import SessionStore, state_hash

# ---- 1. 草案随存档持久化 ---------------------------------------------------------------


def _draft(service: GameService, session_id: str, **kwargs) -> dict:
    return asyncio.run(service.draft_edict(session_id, use_provider=False, **kwargs))


def test_draft_survives_process_restart_and_does_not_touch_state_hash(mech_env) -> None:
    first = GameService()
    session_id, session = first.create_session("mech_test", 3)
    head, digest = session.head_id, state_hash(session.state)
    receipt = _draft(first, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}])
    assert session.head_id == head and state_hash(session.state) == digest  # 拟诏不改日志、不改状态
    assert (mech_env / "sessions" / f"{session_id}.drafts.json").is_file()

    restarted = GameService()  # 新进程：内存里没有任何草案
    confirmed = restarted.confirm_edict(session_id, receipt["id"])
    assert confirmed["confirmed"] and not confirmed["changed_clauses"]
    assert restarted.session(session_id).state.flags["memorial:m_tax"] == "approve"
    with pytest.raises(GameError) as again:
        GameService().confirm_edict(session_id, receipt["id"])
    assert again.value.code == "EDICT_CONFIRMED"


def test_stale_draft_after_turn_advance_is_rejected(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 3)
    receipt = _draft(service, session_id, ops=[{"op": "adjust_resource", "resource": "treasury", "delta": -10}])
    asyncio.run(service.end_turn(session_id, narrate=False))
    with pytest.raises(GameError) as stale:
        GameService().confirm_edict(session_id, receipt["id"])
    assert stale.value.code == "EDICT_STALE"


def test_confirm_revalidates_when_state_changed_since_drafting(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 3)
    adopted = [{"memorial_id": "m_tax", "option_id": "approve"}]
    first = _draft(service, session_id, adopted=adopted)
    second = _draft(
        service,
        session_id,
        adopted=adopted,
        ops=[{"op": "adjust_resource", "resource": "treasury", "delta": -10}],
    )
    GameService().confirm_edict(session_id, first["id"])
    result = GameService().confirm_edict(session_id, second["id"])
    assert result["state_changed"] is True
    memorial_clauses = [clause for clause in result["clauses"] if clause["source"] == "memorial"]
    assert memorial_clauses and all(clause["status"] == "rejected" and clause["changed"] for clause in memorial_clauses)
    assert all(clause["code"] == "MEMORIAL_UNAVAILABLE" for clause in memorial_clauses)
    assert result["changed_clauses"] == [index for index, clause in enumerate(result["clauses"], start=1) if clause.get("changed")]
    client_clause = next(clause for clause in result["clauses"] if clause["source"] == "client")
    assert client_clause["status"] == "applied" and not client_clause.get("changed")
    state = service.session(session_id).state
    assert state.resources["treasury"].amount == 1000 - 50 - 10  # 奏疏只执行一次


def test_mcp_draft_and_confirm_in_separate_server_processes(mech_env) -> None:
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "SIM_SCENARIOS_DIR": str(FIXTURE_SCENARIOS), "LLM_SIM_HOME": str(mech_env), "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "cli.main", "mcp"], env=env, cwd=str(backend))

    async def one_call(name: str, **arguments) -> str:
        async with Client(params, read_timeout_seconds=60) as client:  # 每次调用都起一个新的服务进程
            result = await client.call_tool(name, arguments)
            text = "\n".join(block.text for block in result.content if getattr(block, "text", None))
            assert not result.is_error, text
            return text

    async def run() -> None:
        session_id = re.search(r"session_id=([0-9a-f]{32})", await one_call("new_game", scenario="mech_test", seed=4)).group(1)
        draft = await one_call("draft_edict", session_id=session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}])
        edict_id = re.search(r"edict_id=(edict-[0-9a-f]+)", draft).group(1)
        confirmed = await one_call("confirm_edict", session_id=session_id, edict_id=edict_id)
        assert "已确认" in confirmed and "m_tax→approve" in confirmed
        report = await one_call("end_turn", session_id=session_id)
        assert "天宝十载夏" in report

    asyncio.run(run())


# ---- 2. 延期按条款组掷一次 -------------------------------------------------------------


def test_delay_roll_is_shared_by_all_ops_of_one_memorial_option(mech_scenario) -> None:
    scenario = dict(mech_scenario)
    scenario["scenario"] = {**mech_scenario["scenario"], "formulas": {**mech_scenario["scenario"]["formulas"], "execution": {"base_delay": 0.5}}}
    scenario["memorials"] = [
        {
            "id": "m_multi",
            "title": "多条款",
            "options": [{"id": "go", "label": "准", "ops": [
                {"op": "adjust_resource", "resource": "treasury", "delta": -1},
                {"op": "adjust_resource", "resource": "grain", "delta": -1},
                {"op": "adjust_resource", "resource": "authority", "delta": 1},
                {"op": "set_flag", "key": "multi", "value": True},
            ]}],
        }
    ]
    state = build_initial_state(scenario)
    ops = [{"op": "adjust_resource", "resource": "treasury", "delta": -1} for _ in range(6)]
    seen_statuses: set[str] = set()
    for seed in range(40):
        receipt = asyncio.run(interpret_edict({}, scenario, state, None, seed, adopted=[{"memorial_id": "m_multi", "option_id": "go"}], client_ops=ops))
        memorial = [clause for clause in receipt["clauses"] if clause["source"] == "memorial"]
        assert len(memorial) == 4 and len({clause["status"] for clause in memorial}) == 1
        assert len({clause["group"] for clause in memorial}) == 1
        client = [clause for clause in receipt["clauses"] if clause["source"] == "client"]
        assert len({clause["group"] for clause in client}) == 6  # 每条客户端 op 各自一组
        seen_statuses.add(memorial[0]["status"])
    assert seen_statuses == {"applied", "delayed"}


def test_delayed_group_is_queued_as_one_pending_clause(mech_env, monkeypatch) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 1)
    definition = service.scenario("mech_test")["scenario"]
    monkeypatch.setitem(definition["formulas"], "execution", {"base_delay": 1.0})
    receipt = _draft(service, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}])
    assert {clause["status"] for clause in receipt["clauses"]} == {"delayed"}
    service.confirm_edict(session_id, receipt["id"])
    pending = service.session(session_id).state.pending_clauses
    assert len(pending) == 1 and len(pending[0].ops) == 2


# ---- 3. 玩家本人不能召对 ---------------------------------------------------------------


def test_player_character_is_not_offered_for_audience(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    briefing = service.briefing(session_id)
    people = briefing.split("## 可召对人物")[1].split("\n")[1]
    assert "皇帝（ruler" not in people and "李相（chancellor" in people
    with pytest.raises(GameError) as refused:
        service.audience_card(session_id, "ruler")
    assert refused.value.code == "PLAYER_CHARACTER"
    with pytest.raises(GameError):
        service.debate_brief(session_id, "边镇", ["ruler", "chancellor"])

    from api.app import create_app

    client = TestClient(create_app())
    response = client.post(f"/api/sessions/{session_id}/chat/ruler", json={"text": "朕问你"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "PLAYER_CHARACTER"


def test_player_character_falls_back_to_player_office_holder(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.rules.pop("player_character", None)
    assert player_character_id(state, {}) is None
    assert player_character_id(state, {"player_office": "sovereign"}) == "ruler"


# ---- 4. 事实文本自足、内部事实不外露 ------------------------------------------------------


def test_fact_texts_name_entities_and_internal_facts_are_hidden(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    _, facts = apply({"op": "move_character", "character": "general", "to_region": "pass"}, state)
    assert facts[0].text == "郭将军自京畿启程赴雄关（约1季）"
    _, facts = apply({"op": "adjust_relation", "a": "plotter", "b": "chancellor", "delta": -20}, state)
    assert facts[0].text == "安节度与李相关系 −20"
    _, facts = apply({"op": "adjust_region", "region": "capital", "stat": "defense", "delta": 5}, state)
    assert facts[0].text == "京畿防务 +5"
    _, facts = apply({"op": "move_army", "army": "pass_army", "to_region": "plain"}, state)
    assert "关军" in facts[0].text and "平原" in facts[0].text
    _, flag_facts = apply({"op": "set_flag", "key": "hedong_to_an", "value": True}, state)
    _, counter_facts = apply({"op": "adjust_counter", "key": "broke", "delta": 1}, state)
    assert flag_facts[0].internal and counter_facts[0].internal

    report = TurnReport(turn=1, facts=[*facts, *flag_facts, *counter_facts])
    public = public_report(report, state, 0) or {}
    assert [fact["text"] for fact in public["facts"]] == [facts[0].text]
    assert all("display" in fact for fact in public["facts"])
    assert [fact.text for fact in redacted_report(report, state).facts] == [facts[0].text]
    # 旧存档里没有 internal 字段的旗标事实也按 kind 隐藏
    legacy = {"turn": 1, "facts": [{"kind": "flag", "text": "旗标改变：hedong_to_an"}]}
    assert (public_report(legacy, state, 0) or {})["facts"] == []

    state.flags["_internal:id_seq"] = 3
    assert "_internal:id_seq" not in public_view(state, 0)["flags"]


def test_turn_report_and_chronicle_exclude_flag_facts(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 2)
    receipt = _draft(service, session_id, ops=[{"op": "set_flag", "key": "secret_plan", "value": True}])
    service.confirm_edict(session_id, receipt["id"])
    result = asyncio.run(service.end_turn(session_id, narrate=False))
    assert "旗标" not in result["memorial"] and "secret_plan" not in result["memorial"]
    assert all(fact["kind"] not in {"flag", "counter"} for fact in result["report"]["facts"])
    briefing = service.briefing(session_id)
    assert "旗标" not in briefing and "_internal" not in briefing and "secret_plan" not in briefing


# ---- 5. 数字格式 ----------------------------------------------------------------------


def test_format_number_drops_trailing_zeros() -> None:
    assert format_number(235.5) == "235.5"
    assert format_number(235.50) == "235.5"
    assert format_number(60.0) == "60"
    assert format_number(1.04) == "1"
    assert format_number(-0.04) == "0"
    assert format_number(235.5, signed=True) == "+235.5"
    assert format_number(-3.25, signed=True) == "-3.2"


def test_player_facing_text_has_at_most_one_decimal(mech_env) -> None:
    from simcore.mcp.server import _turn_text

    service = GameService()
    session_id, _ = service.create_session("mech_test", 5)
    receipt = _draft(service, session_id, ops=[{"op": "adjust_resource", "resource": "treasury", "delta": -12.345}])
    assert "12.3" in receipt["clauses"][0]["summary"] and "12.345" not in receipt["clauses"][0]["summary"]
    service.confirm_edict(session_id, receipt["id"])
    texts = [service.briefing(session_id), service.digest(session_id)]
    for _ in range(3):
        result = asyncio.run(service.end_turn(session_id, narrate=False))
        texts.extend([_turn_text(result), service.briefing(session_id), service.digest(session_id)])
        texts.extend(line["display"] for line in result["report"]["ledger_lines"])
        texts.extend(fact["display"] for fact in result["report"]["facts"])
    for text in texts:
        assert not re.search(r"\d\.\d{2,}", text), re.search(r".{0,20}\d\.\d{2,}.{0,10}", text).group(0)
        assert not re.search(r"\d\.0(?!\d)", text), text


# ---- 6. 头衔 -------------------------------------------------------------------------


def test_audience_list_shows_title_when_no_office(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    briefing = service.briefing(session_id)
    assert "郭将军（general，郡王）" in briefing
    assert "李相（chancellor，宰相）" in briefing


# ---- 7. 性能：头状态缓存、快速复制、场景缓存 -------------------------------------------------


def test_head_state_is_cached_and_not_replayed(tmp_path, mech_scenario, monkeypatch) -> None:
    initial = build_initial_state(mech_scenario)
    store = SessionStore(tmp_path / "s.jsonl", mech_scenario, initial, 7)
    for _ in range(4):
        store.append_op({"op": "adjust_resource", "resource": "treasury", "delta": -1})
        store.append_turn()
    calls = {"n": 0}
    original = turn_module.resolve_turn

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("simcore.store.session.resolve_turn", counting)
    for _ in range(20):
        state = store.state
        state.resources["treasury"].amount = -999  # 调用方拿到的是副本，改了也不影响存档
    assert calls["n"] == 0
    assert store.state.resources["treasury"].amount != -999
    reopened = SessionStore(tmp_path / "s.jsonl", mech_scenario)
    assert state_hash(reopened.state) == state_hash(store.state)  # 只追加写入的日志可完整重放
    assert reopened.replay()[1] == state_hash(store.state)


def test_clone_matches_deep_copy(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.flags["nested"] = {"a": [1, {"b": 2}]}
    copied = clone(state)
    assert copied == state.model_copy(deep=True) and isinstance(copied, WorldState)
    copied.flags["nested"]["a"][1]["b"] = 3
    copied.characters["general"].traits["ability"] = 0
    assert state.flags["nested"]["a"][1]["b"] == 2 and state.characters["general"].traits["ability"] == 7


def test_load_scenario_is_cached_but_returns_independent_copies() -> None:
    first = load_scenario(FIXTURE_SCENARIOS / "mech_test")
    first["scenario"]["title"] = "改过"
    second = load_scenario(FIXTURE_SCENARIOS / "mech_test")
    assert second["scenario"]["title"] == "机制测试"


def test_resolve_turn_inplace_matches_copy_semantics(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    before = state_hash(state)
    updated, report = turn_module.resolve_turn(state, random.Random(3), mech_scenario)
    assert state_hash(state) == before  # 结算不修改传入状态
    assert report.state_hash == state_hash(updated)
