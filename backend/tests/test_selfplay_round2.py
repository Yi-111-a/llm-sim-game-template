"""《天宝》第二轮自玩（self-02）发现的引擎问题的回归测试：

#8 廷议立场随议题、#9 摘要去掉外域经济指标与按人物筛选、#11 存档快照、#12 事实带原因、#13 多段行程、
#16 处置写入记忆、#17 摘要不出 None、#18 end_turn 不重复终局、#19 难度档。
"""

import asyncio
import json
import random
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURE_SCENARIOS
from fastapi.testclient import TestClient
from mcp import Client

from api.app import create_app
from simcore.agent.debate import decide_stances, topic_entities
from simcore.domain.apply import apply
from simcore.domain.digest import render_digest
from simcore.domain.memorials import summarize_op, summarize_ops
from simcore.domain.models import Region, TurnReport, WorldState
from simcore.domain.rules import Rejected, find_route, validate
from simcore.domain.turn import resolve_turn
from simcore.domain.visibility import public_report
from simcore.mcp.server import _turn_text, build_server
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.service import GameError, GameService
from simcore.store.session import SessionStore, state_hash

DIFFICULTIES = {
    "difficulties": [
        {"id": "easy", "label": "宽仁", "description": "国用宽裕，危机缓进", "drift_scale": {"risk": 0}, "start": {"treasury": 2000}},
        {"id": "hard", "label": "多事", "description": "危机倍进，事件皆免", "drift_scale": {"risk": 2}, "gravity_scale": {"*": 0}, "delay_scale": 2},
    ],
    "default_difficulty": "easy",
}


@pytest.fixture
def scenario_dir(tmp_path, monkeypatch) -> Path:
    """可改动的 mech_test 副本（带难度档），存档放在临时目录。"""
    root = tmp_path / "scenarios"
    shutil.copytree(FIXTURE_SCENARIOS / "mech_test", root / "mech_test")
    path = root / "mech_test" / "scenario.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(DIFFICULTIES)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("SIM_SCENARIOS_DIR", str(root))
    monkeypatch.setenv("LLM_SIM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIM_DEBUG", raising=False)
    monkeypatch.delenv("SIM_MCP_MODE", raising=False)
    return root / "mech_test"


def _draft(service: GameService, session_id: str, **kwargs) -> dict:
    return asyncio.run(service.draft_edict(session_id, use_provider=False, **kwargs))


# ---- #11 存档快照 ---------------------------------------------------------------------------


def test_turn_entries_carry_compact_snapshots_and_header_fingerprint(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 4)
    asyncio.run(service.end_turn(session_id, narrate=False))
    lines = session.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["scenario_version"] == "0.1.0" and len(header["scenario_hash"]) == 64
    turn = next(json.loads(line) for line in lines[1:] if json.loads(line)["type"] == "turn")
    assert turn["payload"]["snapshot"]["turn"] == 1
    snapshot = WorldState.model_validate(turn["payload"]["snapshot"])
    assert state_hash(snapshot) == turn["payload"]["state_hash"]
    assert all('", "' not in line and '": ' not in line for line in lines)  # 紧凑 JSON


def test_loading_uses_snapshots_not_the_current_scenario(scenario_dir) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 4)
    asyncio.run(service.end_turn(session_id, narrate=False))  # 第 1 回合：边将请饷 → 待决事件
    _draft(service, session_id, ops=[{"op": "adjust_resource", "resource": "treasury", "delta": -5}])
    before = service.session(session_id).state
    assert [row.event_id for row in before.pending_choices] == ["petition"]

    # 剧本改动：删掉 petition 事件并改了版本号。旧逻辑重放时待决事件会消失。
    events = [row for row in yaml.safe_load((scenario_dir / "events.yaml").read_text(encoding="utf-8")) if row["id"] != "petition"]
    (scenario_dir / "events.yaml").write_text(yaml.safe_dump(events, allow_unicode=True), encoding="utf-8")
    data = yaml.safe_load((scenario_dir / "scenario.yaml").read_text(encoding="utf-8"))
    data["version"] = "0.2.0"
    (scenario_dir / "scenario.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    reloaded = GameService()
    state = reloaded.session(session_id).state
    assert state_hash(state) == state_hash(before)
    assert [row.event_id for row in state.pending_choices] == ["petition"]
    view = reloaded.view(session_id)
    assert view["scenario_changed"]["saved_version"] == "0.1.0" and view["scenario_changed"]["current_version"] == "0.2.0"
    assert view["warnings"][0]["code"] == "scenario_changed"
    assert "scenario_changed" in reloaded.briefing(session_id)
    # 旧事件的待决抉择仍可处置；之后的回合按新剧本结算
    reloaded.choose(session_id, "choice-petition-1", "grant")
    asyncio.run(reloaded.end_turn(session_id, narrate=False))

    # 完整重放只作校验：报告分歧，不改存档
    raw = reloaded.session(session_id).path.read_text(encoding="utf-8")
    result = reloaded.session(session_id).verify_replay()
    assert result["turns_checked"] == 2 and result["scenario_changed"] is not None
    assert reloaded.session(session_id).path.read_text(encoding="utf-8") == raw


def test_verify_replay_ok_and_old_saves_without_snapshots_still_load(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 9)
    for _ in range(3):
        asyncio.run(service.end_turn(session_id, narrate=False))
    expected = state_hash(session.state)
    assert session.verify_replay()["ok"] is True
    # 旧存档：去掉快照与指纹
    lines = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    for row in lines:
        row.get("payload", {}).pop("snapshot", None)
        row.pop("scenario_hash", None)
    session.path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in lines), encoding="utf-8")
    old = SessionStore(session.path, load_scenario(FIXTURE_SCENARIOS / "mech_test"))
    assert state_hash(old.state) == expected and old.scenario_changed is None


# ---- #8 廷议立场随议题 ------------------------------------------------------------------------


def test_debate_uses_memorial_advisor_stances_by_id_or_fuzzy_title(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    by_id = {row["character"]: row for row in decide_stances(state, mech_scenario, "此事", ["chancellor", "eunuch"], memorial_id="m_tax")}
    assert by_id["chancellor"]["stance"] == "oppose" and by_id["eunuch"]["stance"] == "support"
    assert "请减赋" in by_id["chancellor"]["reason"]
    fuzzy = {row["character"]: row["stance"] for row in decide_stances(state, mech_scenario, "议请减赋事", ["chancellor", "eunuch"])}
    assert fuzzy == {"chancellor": "oppose", "eunuch": "support"}


def test_debate_stance_depends_on_entities_named_in_topic(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.characters["chancellor"].traits["relation:plotter"] = -50
    assert [row["id"] for row in topic_entities(state, "加安节度兵权，移驻北镇")] == ["plotter", "north"]
    grant = decide_stances(state, mech_scenario, "加安节度兵权", ["chancellor", "plotter"])
    curb = decide_stances(state, mech_scenario, "罢安节度兵权", ["chancellor", "plotter"])
    assert [row["stance"] for row in grant] == ["oppose", "support"]
    assert [row["stance"] for row in curb] == ["support", "oppose"]
    assert "嫌隙" in grant[0]["reason"] and grant[1]["reason"] == "事关本人权位"
    # 同一人对不同议题立场不同（self-02：哥舒翰对禄山之请「持中」）
    praise = decide_stances(state, mech_scenario, "赏李相", ["chancellor"])[0]
    assert praise["stance"] == "support"
    # 无利害时种子决断：同一局同一议题稳定
    first = decide_stances(state, mech_scenario, "议礼", ["chancellor"], seed=5)
    assert first == decide_stances(state, mech_scenario, "议礼", ["chancellor"], seed=5)
    assert first[0]["stance"] in {"support", "oppose", "neutral"}


def test_debate_api_and_mcp_accept_memorial_id_and_expose_topic_entities(mech_env) -> None:
    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 2}).json()["id"]
    response = client.post(f"/api/sessions/{session_id}/debate", json={"memorial_id": "m_tax", "participants": ["chancellor", "eunuch"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["topic"] == "请减赋" and body["memorial"]["id"] == "m_tax"
    assert [row["stance"] for row in body["speeches"]] == ["oppose", "support"]
    assert body["topic_entities"] == []
    missing = client.post(f"/api/sessions/{session_id}/debate", json={"memorial_id": "ghost", "participants": ["chancellor"]})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "UNKNOWN_MEMORIAL"

    async def run() -> None:
        async with Client(build_server(GameService(), mode="client")) as mcp_client:
            result = await mcp_client.call_tool(
                "court_debate", {"session_id": session_id, "topic": "罢安节度", "participants": ["chancellor", "general"], "memorial_id": ""}
            )
            text = "\n".join(block.text for block in result.content)
            assert not result.is_error and "议题涉及：安节度（人物）" in text

    asyncio.run(run())


# ---- #9 摘要 ---------------------------------------------------------------------------------


def test_digest_omits_foreign_economy_and_filters_by_character_relevance(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    digest = render_digest(state, 1, None, None, mech_scenario)
    region_line = next(line for line in digest.splitlines() if line.startswith("【地区】"))
    foreign_line = next(line for line in digest.splitlines() if line.startswith("【外域】"))
    assert "北镇" not in region_line and "京畿" in region_line and "繁荣" in region_line
    assert foreign_line == "【外域】北镇（叛军）驻叛军主力" and "繁荣" not in foreign_line

    state.characters["chancellor"].knowledge.append("平原")
    personal = render_digest(state, 1, "chancellor", None, mech_scenario)
    lines = {line[: line.index("】") + 1]: line for line in personal.splitlines()}
    assert "平原" in lines["【地区】"] and "京畿" in lines["【地区】"]  # 所在 + 所知
    assert lines["【地区】"].index("京畿") < lines["【地区】"].index("平原")
    assert lines["【他处】"] == "【他处】叛军：北镇"


# ---- #12 事实带原因 -------------------------------------------------------------------------------


def test_resource_facts_carry_reason_or_source(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    _, facts = apply({"op": "adjust_resource", "resource": "treasury", "delta": -40}, state, source="遣中使察看北镇")
    assert facts[0].text == "国库 −40（遣中使察看北镇）" and facts[0].cause == "遣中使察看北镇"
    _, facts = apply({"op": "adjust_resource", "resource": "treasury", "delta": -40, "reason": "减赋"}, state, source="请减赋")
    assert facts[0].text == "国库 −40（减赋）"
    _, hidden = apply({"op": "adjust_resource", "resource": "ambition", "delta": 3}, state, source="厚赐")
    public = public_report(TurnReport(turn=0, facts=hidden), state, 0) or {}
    assert public["facts"][0]["text"] == "异志有所变动（厚赐；数值不明）"


def test_turn_facts_name_event_order_project_and_delayed_edict_sources(mech_scenario) -> None:
    scenario = json.loads(json.dumps(mech_scenario))
    scenario["events"].append({"id": "tribute", "title": "西域进贡", "gravity": 1, "effects": [{"op": "adjust_resource", "resource": "treasury", "delta": 30}]})
    state = build_initial_state(scenario)
    state, _ = apply({"op": "add_standing_order", "text": "常平仓", "ops": [{"op": "adjust_resource", "resource": "grain", "delta": 5}]}, state)
    state, _ = apply(
        {"op": "start_project", "name": "修城", "region": "capital", "cost_per_turn": {"treasury": 1}, "turns": 1,
         "on_complete": [{"op": "adjust_region", "region": "capital", "stat": "defense", "delta": 5}]},
        state,
    )
    from simcore.domain.apply import queue_delayed

    state = queue_delayed(state, [{"op": "adjust_resource", "resource": "treasury", "delta": -10}], 1, label="请减赋")
    _, report = resolve_turn(state, random.Random(1), scenario)
    texts = [fact.text for fact in report.facts]
    assert "国库 −10（请减赋）" in texts
    assert "粮储 +5（常设令：常平仓）" in texts
    assert "京畿防务 +5（修城竣工）" in texts
    assert "国库 +30（西域进贡）" in texts


# ---- #13 多段行程 ---------------------------------------------------------------------------------


def test_multi_step_travel_finds_route_and_travels_as_one_transit(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state, facts = apply({"op": "move_character", "character": "general", "to_region": "north"}, state)
    assert facts[0].text == "郭将军自京畿启程赴北镇（经雄关、平原，约3季）"
    transit = state.characters["general"].in_transit
    assert transit["route"] == ["capital", "pass", "plain", "north"] and transit["eta_turn"] == 3
    for _ in range(2):
        state, _ = resolve_turn(state, random.Random(0), mech_scenario)
        assert state.characters["general"].region == "capital"
    state, report = resolve_turn(state, random.Random(0), mech_scenario)
    assert state.characters["general"].region == "north" and "郭将军抵达北镇" in [fact.text for fact in report.facts]
    assert summarize_op({"op": "move_army", "army": "guard", "to_region": "plain"}, state) == "禁军 移驻 平原（经雄关，约2季）"


def test_route_prefers_own_regions_and_rejects_unreachable(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.regions["alt"] = Region(id="alt", name="别道", controller="rebels", adjacency={"capital": 1, "plain": 1})
    state.regions["capital"].adjacency["alt"] = 1
    state.regions["plain"].adjacency["alt"] = 1
    assert find_route(state, "capital", "plain", "court") == (["capital", "pass", "plain"], 2)
    assert find_route(state, "capital", "plain", "rebels") == (["capital", "alt", "plain"], 2)
    state.regions["island"] = Region(id="island", name="孤岛")
    rejected = validate({"op": "move_army", "army": "guard", "to_region": "island"}, state)
    assert isinstance(rejected, Rejected) and rejected.code == "OUT_OF_RANGE" and "无路可达" in rejected.message


# ---- #16 处置写入记忆 ----------------------------------------------------------------------------------


def test_memorial_and_choice_memory_and_edict_auto_memories(scenario_dir) -> None:
    for name, mutate in (
        ("memorials.yaml", lambda rows: rows[0]["options"][0].__setitem__("memory", {"character": "chancellor", "text": "陛下准减赋，未从臣议"})),
        ("events.yaml", lambda rows: rows[0]["choices"][0].__setitem__("memory", [{"character": "plotter", "text": "朝廷准我增饷"}])),
    ):
        path = scenario_dir / name
        rows = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(rows)
        path.write_text(yaml.safe_dump(rows, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert validate_scenario(scenario_dir) == []
    service = GameService()
    session_id, _ = service.create_session("mech_test", 3)
    receipt = _draft(
        service,
        session_id,
        adopted=[{"memorial_id": "m_tax", "option_id": "approve"}],
        ops=[{"op": "appoint", "character": "chancellor", "office": "censor"}, {"op": "dismiss", "character": "chancellor", "office": "chancellor"}],
    )
    assert "李相 记下：陛下准减赋，未从臣议" in [clause["summary"] for clause in receipt["clauses"]]
    service.confirm_edict(session_id, receipt["id"])
    memories = service.session(session_id).state.characters["chancellor"].memories
    assert "陛下准减赋，未从臣议" in memories
    assert any(item.endswith("陛下任李相为御史") for item in memories)
    assert any(item.endswith("陛下免李相宰相之职") for item in memories)
    card = service.audience_card(session_id, "chancellor")
    assert "【记忆】" in card["digest"] and "陛下准减赋" in card["digest"]
    asyncio.run(service.end_turn(session_id, narrate=False))
    service.choose(session_id, "choice-petition-1", "grant")
    assert "朝廷准我增饷" in service.session(session_id).state.characters["plotter"].memories


def test_memory_validation_rejects_unknown_character(mech_scenario) -> None:
    data = json.loads(json.dumps(mech_scenario))
    data["memorials"][0]["options"][1]["memory"] = {"character": "ghost", "text": "x"}
    assert any("memory 人物不存在：ghost" in error for error in validate_scenario(data))


def test_delayed_edict_appointment_writes_memory_when_executed(mech_scenario) -> None:
    from simcore.domain.apply import queue_delayed

    state = build_initial_state(mech_scenario)
    state = queue_delayed(state, [{"op": "appoint", "character": "general", "office": "marshal"}], 1, label="诏令")
    state, _ = resolve_turn(state, random.Random(0), mech_scenario)
    assert any("陛下任郭将军为元帅" in item for item in state.characters["general"].memories)


# ---- #17 摘要不出现 None ------------------------------------------------------------------------------------


def test_summaries_and_facts_never_print_none(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    assert summarize_op({"op": "set_army_commander", "army": "guard", "character": None}, state) == "罢禁军统帅"
    _, facts = apply({"op": "set_army_commander", "army": "guard", "character": None}, state)
    assert facts[0].text == "罢禁军统帅"
    ops = [
        {"op": "set_army_commander", "army": "guard"},
        {"op": "cancel_ledger", "ledger_id": "x"},
        {"op": "add_standing_order", "text": "常平", "ops": []},
        {"op": "cancel_standing_order", "id": "y"},
        {"op": "set_region_controller", "region": "north", "faction": "court"},
        {"op": "choose_event_option", "choice_id": "c", "option_id": "o"},
        {"op": "move_army", "army": "ghost", "to_region": "north"},
    ]
    assert all("None" not in line and "null" not in line for line in summarize_ops(ops, state))


# ---- #18 end_turn 不重复 --------------------------------------------------------------------------------------


def test_end_turn_text_shows_outcome_and_ledger_header_once(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 1)
    session.append_op({"op": "set_region_controller", "region": "capital", "faction": "rebels"})
    result = asyncio.run(service.end_turn(session_id, narrate=False))
    assert result["outcome"]["id"] == "fail_capital"
    text = _turn_text(result, show_memorial=False)
    assert text.count("京城不守") == 1 and text.count("终局") == 1 and text.count("账目合计") == 1
    assert "## 纪事" not in text
    narrated = _turn_text({**result, "memorial": "京城陷落。"})
    assert narrated.count("终局") == 1 and "## 纪事\n京城陷落。" in narrated


# ---- #19 难度档 -----------------------------------------------------------------------------------------


def test_difficulty_validation(mech_scenario) -> None:
    data = json.loads(json.dumps(mech_scenario))
    data["scenario"].update(json.loads(json.dumps(DIFFICULTIES)))
    assert validate_scenario(data) == []
    bad = json.loads(json.dumps(data))
    bad["scenario"]["default_difficulty"] = "nightmare"
    bad["scenario"]["difficulties"][0]["drift_scale"] = {"ghost": 1, "risk": -1}
    bad["scenario"]["difficulties"][1]["gravity_scale"] = {"no_event": 0.5}
    bad["scenario"]["difficulties"].append({"id": "easy", "label": "重复"})
    errors = validate_scenario(bad)
    expectations = (
        "default_difficulty 不在 difficulties 中",
        "drift_scale 资源不存在：ghost",
        "drift_scale.risk 必须是非负数",
        "gravity_scale 事件不存在：no_event",
        "难度 easy id 重复",
    )
    for expected in expectations:
        assert any(expected in error for error in errors), (expected, errors)


def test_difficulty_scales_start_drift_gravity_and_delay(mech_scenario) -> None:
    data = json.loads(json.dumps(mech_scenario))
    data["scenario"].update(json.loads(json.dumps(DIFFICULTIES)))
    data["scenario"]["formulas"]["execution"] = {"base_delay": 0.3}
    easy, hard = build_initial_state(data), build_initial_state(data, "hard")
    assert easy.rules["difficulty"]["id"] == "easy" and easy.resources["treasury"].amount == 2000
    assert hard.resources["treasury"].amount == 1000
    plain = build_initial_state(mech_scenario)
    assert "difficulty" not in plain.rules
    for state in (plain, easy, hard):
        state.resources["ambition"].amount = 60  # risk 漂移 = 0.1*(60-40) = 2
    risk = {name: resolve_turn(state, random.Random(0), data)[0].resources["risk"].amount for name, state in (("plain", plain), ("easy", easy), ("hard", hard))}
    assert risk["easy"] == 30 and risk["plain"] == pytest.approx(32) and risk["hard"] == pytest.approx(34)
    fired = {name: resolve_turn(state, random.Random(0), data)[1].events_fired for name, state in (("easy", easy), ("hard", hard))}  # petition: when turn 1
    assert "petition" in fired["easy"] and fired["hard"] == []
    from simcore.agent.roles import _delay_probability

    assert _delay_probability({"base_delay": 0.3}, hard) == pytest.approx(0.6)
    assert _delay_probability({"base_delay": 0.3}, easy) == pytest.approx(0.3)
    with pytest.raises(ValueError, match="未知难度"):
        build_initial_state(data, "nightmare")


def test_difficulty_through_service_api_and_mcp(scenario_dir) -> None:
    service = GameService()
    scenarios = {row["folder"]: row for row in service.list_scenarios()}
    assert [row["id"] for row in scenarios["mech_test"]["difficulties"]] == ["easy", "hard"]
    session_id, session = service.create_session("mech_test", 1, "hard")
    assert session.header["difficulty"] == "hard"
    view = service.view(session_id)
    assert view["difficulty"] == {"id": "hard", "label": "多事", "description": "危机倍进，事件皆免"} and view["scenario_changed"] is None
    assert "难度：多事（hard）" in service.briefing(session_id)
    assert service.list_sessions()[0]["difficulty"] == "hard"
    with pytest.raises(GameError) as unknown:
        service.create_session("mech_test", 1, "nightmare")
    assert unknown.value.code == "UNKNOWN_DIFFICULTY"

    client = TestClient(create_app())
    created = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 1, "difficulty": "easy"})
    assert created.status_code == 200 and created.json()["difficulty"]["id"] == "easy"
    assert client.post("/api/sessions", json={"scenario": "mech_test", "difficulty": "nightmare"}).json()["detail"]["code"] == "UNKNOWN_DIFFICULTY"

    async def run() -> None:
        async with Client(build_server(GameService(), mode="client")) as mcp_client:
            result = await mcp_client.call_tool("new_game", {"scenario": "mech_test", "seed": 2, "difficulty": "hard"})
            text = "\n".join(block.text for block in result.content)
            assert not result.is_error and "难度：多事（hard）" in text

    asyncio.run(run())


def test_summaries_hide_hidden_values_and_bloc_ids(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    assert summarize_op({"op": "adjust_resource", "resource": "ambition", "delta": -2}, state) == "异志 ↓（风闻）"
    assert summarize_op({"op": "adjust_trait", "character": "plotter", "trait": "ambition", "delta": 2}, state) == "安节度 心态有变（不明）"
    stance = decide_stances(state, mech_scenario, "边镇增兵议", ["chancellor"])[0]
    assert stance["reason"] == "本派的共同立场" and "xiang" not in stance["reason"]
    state.rules["bloc_names"] = {"xiang": "相党"}
    assert decide_stances(state, mech_scenario, "边镇增兵议", ["chancellor"])[0]["reason"] == "相党一系的共同立场"


def test_reloaded_state_keeps_scenario_order(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 4)
    asyncio.run(service.end_turn(session_id, narrate=False))
    expected = list(session.state.resources)
    reloaded = SessionStore(session.path, load_scenario(FIXTURE_SCENARIOS / "mech_test"))
    assert list(reloaded.initial_state.resources) == expected == ["treasury", "grain", "authority", "corruption", "ambition", "risk"]
    assert list(reloaded.state.resources) == expected and list(reloaded.state.armies) == list(session.state.armies)
