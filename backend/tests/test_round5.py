"""第 5 轮（GPT gpt-03 报告）引擎修复：#39 任命一条规则、#40 官职兵权总表、#42 廷议立场指向选项、#43 危机变化拆分、
#45 经常收支明细与取整差、#46 终局季提示、#48 召对方式、#49 结构化导出，以及 army_commander / army_region 条件。"""

import asyncio
import copy
import json
import random

import pytest
from conftest import FIXTURE_SCENARIOS
from fastapi.testclient import TestClient

from simcore.agent.debate import decide_stances
from simcore.agent.roles import minister_chat_messages, offline_minister_text
from simcore.agent.tools import PLAYER_TIER, SCENARIO_TIER, AgentTools
from simcore.domain.apply import apply
from simcore.domain.describe import describe_condition
from simcore.domain.events import condition_errors, matches
from simcore.domain.memorials import summarize_ops
from simcore.domain.reference import APPOINTMENT_RULE, build_reference
from simcore.domain.rules import audience_mode
from simcore.domain.turn import final_season_notice, judge_outcome, resolve_turn
from simcore.domain.visibility import public_crisis_change, public_reconciliation
from simcore.mcp.server import _turn_text, build_server
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.service import GameService


def _draft(service: GameService, session_id: str, **kwargs) -> dict:
    return asyncio.run(service.draft_edict(session_id, use_provider=False, **kwargs))


def _posted_scenario() -> dict:
    """mech_test 加一个有任所的官职：关镇节度（统关军）。"""
    scenario = load_scenario(FIXTURE_SCENARIOS / "mech_test")
    scenario["scenario"]["offices"].append({"id": "pass_jds", "name": "关镇节度", "army": "pass_army"})
    return scenario


def _service_with(scenario: dict) -> GameService:
    service = GameService()
    service.scenarios["mech_test"] = scenario
    return service


# ---- #39 任命只有一条规则 ------------------------------------------------------------------------------


def test_player_commander_dispatches_and_scenario_commander_is_remote(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    tools = AgentTools(state)
    player = tools.propose_ops([{"op": "set_army_commander", "army": "pass_army", "character": "eunuch"}], tier=PLAYER_TIER)[0]
    assert player["op"]["dispatch"] is True
    assert "已启程赴任（自京畿，约1季抵雄关）" in summarize_ops([player["op"]], state)[0]
    dispatched, facts = apply(player["op"], state)
    assert dispatched.characters["eunuch"].in_transit["to_region"] == "pass"
    assert "已启程赴任（" in facts[0].text and "到任前由副将暂摄" in facts[0].text
    scenario_op = AgentTools(state).propose_ops([{"op": "set_army_commander", "army": "pass_army", "character": "eunuch"}], tier=SCENARIO_TIER)[0]
    assert scenario_op["op"]["dispatch"] is False
    assert "遥领，不赴任（今在京畿），由副将暂摄" in summarize_ops([scenario_op["op"]], state)[0]
    remote, facts = apply(scenario_op["op"], state)
    assert remote.characters["eunuch"].in_transit is None and "遥领，不赴任（今在京畿）" in facts[0].text
    # 已在军中
    _, facts = apply({"op": "set_army_commander", "army": "guard", "character": "chancellor"}, state)
    assert "人在军中，即日视事" in facts[0].text


def test_appoint_to_commander_office_dispatches_for_player_and_sets_army() -> None:
    scenario = _posted_scenario()
    assert validate_scenario(scenario) == []
    state = build_initial_state(scenario)
    assert state.rules["office_postings"] == {"pass_jds": {"army": "pass_army"}}
    tools = AgentTools(state)
    receipt = tools.propose_ops([{"op": "appoint", "character": "general", "office": "pass_jds"}], tier=PLAYER_TIER)[0]
    assert receipt["status"] == "applied" and receipt["op"]["dispatch"] is True
    summary = summarize_ops([receipt["op"]], state)[0]
    assert "兼统关军" in summary and "已启程赴任（" in summary
    updated, facts = apply(receipt["op"], state)
    assert updated.armies["pass_army"].commander == "general" and updated.characters["general"].in_transit["to_region"] == "pass"
    assert "郭将军任关镇节度，兼统关军；已启程赴任（" in facts[0].text
    # 其原统禁军（同驻京畿）改由副将暂摄
    assert "其原统禁军改由副将暂摄" in facts[0].text
    # 剧本 / 事件的任命缺省遥领
    remote, facts = apply({"op": "appoint", "character": "general", "office": "pass_jds"}, state)
    assert remote.characters["general"].in_transit is None and "遥领，不赴任（今在京畿）" in facts[0].text
    # 免职即罢其统帅
    dismissed, facts = apply({"op": "dismiss", "character": "general", "office": "pass_jds"}, remote)
    assert dismissed.armies["pass_army"].commander is None and "罢统关军" in facts[0].text
    # 校验：官职的 army / seat 须存在
    broken = copy.deepcopy(scenario)
    broken["scenario"]["offices"][-1]["army"] = "ghost"
    assert any("所统军队不存在" in error for error in validate_scenario(broken))


def test_op_reference_states_the_same_appointment_rule(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    reference = build_reference(state)
    assert APPOINTMENT_RULE in reference["rules"]["notes"]
    assert "已启程赴任（" in APPOINTMENT_RULE and "遥领，不赴任" in APPOINTMENT_RULE
    assert build_reference(state, op="appoint")["note"] == APPOINTMENT_RULE
    assert "dispatch?: bool" in build_reference(state, op="appoint")["fields"]


# ---- #40 官职—所在—所统—指挥状态 ---------------------------------------------------------------------


def test_command_table_in_view_and_briefing(mech_env) -> None:
    service = _service_with(_posted_scenario())
    session_id, session = service.create_session("mech_test", 3)
    session.append_op({"op": "set_army_commander", "army": "pass_army", "character": "plotter"})  # 遥领：人在北镇
    view = service.view(session_id)
    table = view["command_table"]
    rows = {row["character"]: row for row in table["rows"]}
    assert rows["general"]["commands"][0]["status"] == "present"
    assert rows["plotter"]["commands"][0]["status"] == "remote" and rows["plotter"]["location"]["name"] == "北镇"
    assert "ruler" not in rows  # 玩家本人不列
    assert {row["id"] for row in table["vacancies"]["offices"]} >= {"marshal", "pass_jds"}
    assert any("安节度遥领关军" in alert for alert in table["alerts"])
    briefing = service.briefing(session_id)
    assert "## 官职与兵权（官职—所在—所统—指挥状态" in briefing
    assert "| 官职 | 人物 | 所在 | 所统 | 指挥状态 |" in briefing and "**遥领（副将暂摄）**" in briefing
    assert "| 关镇节度 | **出缺** |" in briefing


# ---- #42 廷议立场指向选项 ------------------------------------------------------------------------------


def test_debate_stances_target_memorial_options(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    stances = {row["character"]: row for row in decide_stances(state, mech_scenario, "", ["chancellor", "eunuch", "general"], memorial_id="m_tax", seed=1)}
    assert stances["eunuch"]["stance"] == "support" and stances["eunuch"]["target_option_id"] == "approve"
    assert stances["chancellor"]["stance"] == "oppose" and stances["chancellor"]["target_option_id"] == "deny"
    assert stances["chancellor"]["against_option_id"] == "approve" and "反对「准减赋」" in stances["chancellor"]["position"]
    assert stances["general"]["target_option_id"] in {"approve", "deny"} and stances["general"]["target_option_label"]
    free = decide_stances(state, mech_scenario, "增筑雄关", ["general"], seed=1)[0]
    assert free["target_option_id"] is None and free["proposition"] == "增筑雄关" and "「增筑雄关」" in free["position"]


def test_debate_option_choice_follows_self_interest(mech_scenario) -> None:
    scenario = copy.deepcopy(mech_scenario)
    scenario["memorials"].append({
        "id": "m_chief",
        "turn": 0,
        "title": "择帅",
        "advisors": [{"character": "chancellor", "stance": "support", "line": "宜用老成。"}],
        "options": [
            {"id": "guo", "label": "以郭将军统关军", "ops": [{"op": "set_army_commander", "army": "pass_army", "character": "general"}]},
            {"id": "an", "label": "以安节度统关军", "ops": [{"op": "set_army_commander", "army": "pass_army", "character": "plotter"}]},
        ],
    })
    state = build_initial_state(scenario)
    rows = {row["character"]: row for row in decide_stances(state, scenario, "", ["general", "plotter"], memorial_id="m_chief", seed=2)}
    assert rows["general"]["target_option_id"] == "guo" and rows["plotter"]["target_option_id"] == "an"


def test_mcp_court_debate_text_names_target_options(mech_env) -> None:
    from mcp.client.client import Client

    async def run() -> str:
        service = GameService()
        session_id, _ = service.create_session("mech_test", 3)
        async with Client(build_server(service, mode="client")) as client:
            result = await client.call_tool("court_debate", {"session_id": session_id, "participants": ["chancellor", "eunuch"], "memorial_id": "m_tax"})
            return "".join(getattr(item, "text", "") for item in result.content)

    text = asyncio.run(run())
    assert "议题命题：就奏疏「请减赋」诸选项择一" in text and "主张「驳回」（deny），反对「准减赋」（approve）" in text


# ---- #43 危机变化拆分 ----------------------------------------------------------------------------------


def _hidden_crisis_scenario() -> dict:
    scenario = load_scenario(FIXTURE_SCENARIOS / "mech_test")
    for row in scenario["scenario"]["resources"]:
        if row["id"] == "risk":
            row.update({"hidden": True, "estimate_width": 6})
    scenario["events"].append({
        "id": "rumor",
        "title": "北镇流言",
        "text": "流言四起。",
        "when": {"turn": 1},
        "gravity": 1,
        "effects": [{"op": "adjust_resource", "resource": "risk", "delta": 6.4, "reason": "北镇流言"}],
    })
    return scenario


def test_crisis_change_splits_drift_difficulty_and_event_shocks() -> None:
    scenario = _hidden_crisis_scenario()
    state = build_initial_state(scenario)
    state.rules["difficulty"] = {"id": "hard", "label": "多事", "drift_scale": {"risk": 1.5}}
    state.resources["ambition"].amount = 50  # 异志 +1/季（隐藏）
    updated, report = resolve_turn(state, random.Random(1), scenario)
    change = report.crisis["change"]
    assert change["drift"]["base"] == pytest.approx(1.0) and change["drift"]["difficulty"] == pytest.approx(0.5)
    assert {"kind": "event", "label": "北镇流言", "amount": pytest.approx(6.4)} in change["items"]
    assert change["closing"] - change["opening"] == pytest.approx(1.5 + 6.4 + change["other"])
    public = public_crisis_change(scenario["scenario"]["crisis"], change, updated, 7)
    assert public["hidden"] is True and public["total"] == [5.0, 8.0]
    shock = next(row for row in public["items"] if row["label"] == "北镇流言")
    assert shock["value"] == [5.0, 8.0] and shock["display"] == "+5～+8"
    assert isinstance(public["drift"]["base"], list) and public["drift"]["difficulty_label"] == "多事"
    text = "\n".join(public["lines"])
    assert "事件冲击「北镇流言」+5～+8" in text and "难度加成（多事 ×1.5）" in text and "非下季预计" in text
    assert "6.4" not in json.dumps(public, ensure_ascii=False)  # 真值不外露


def test_crisis_change_reaches_end_turn_text_report_and_briefing(mech_env) -> None:
    service = _service_with(_hidden_crisis_scenario())
    session_id, _ = service.create_session("mech_test", 3)
    receipt = _draft(service, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}])
    service.confirm_edict(session_id, receipt["id"])
    result = asyncio.run(service.end_turn(session_id, narrate=False))
    change = result["report"]["crisis_change"]
    assert change and any(row["label"] == "北镇流言" for row in change["items"])
    text = _turn_text(result, show_memorial=False)
    assert "## 本季危机变化（结算当时的实际贡献）" in text and "事件冲击「北镇流言」" in text
    assert "分项（按结算后状态计算，即下次结算的预计漂移）" in text
    briefing = service.briefing(session_id)
    assert "## 上季危机变化（结算当时的实际贡献）" in briefing and "事件冲击「北镇流言」" in briefing
    assert service.view(session_id)["last_report"]["crisis_change"] == change


# ---- #45 经常收支明细与取整差 ---------------------------------------------------------------------------


def test_reconciliation_rounding_is_its_own_line_not_folded_into_edicts(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    raw = {"authority": {"opening": 60.04, "edicts": -2.0, "delayed": 0.0, "regular": 0.04, "events": 0.0, "closing": 58.08}}
    row = public_reconciliation(raw, state)[0]
    assert row["shown"]["edicts"] == -2.0  # 旧做法把取整余差并入最大的一段，显示成 −1.9
    assert row["shown"]["rounding"] == pytest.approx(0.1)
    steps = ("edicts", "delayed", "regular", "events", "rounding")
    assert round(row["shown"]["opening"] + sum(row["shown"].get(step, 0) for step in steps), 1) == row["shown"]["closing"]
    assert "诏令 −2 →" in row["display"] and "取整差 +0.1 → 期末 58.1" in row["display"]


def test_regular_income_is_itemised_and_sums_exactly(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 3)
    session.append_op({"op": "add_ledger", "name": "边军月饷", "category": "treasury", "amount": -12.35, "kind": "recurring"})
    session.append_op({"op": "start_project", "name": "修城", "region": "capital", "cost_per_turn": {"treasury": 7.07}, "turns": 2})
    result = asyncio.run(service.end_turn(session_id, narrate=False))
    rows = {row["resource"]: row for row in result["report"]["reconciliation"]}
    treasury = rows["treasury"]
    labels = {item["label"]: item for item in treasury["regular_detail"]}
    assert {"税入", "边军月饷", "修城施工"} <= set(labels)
    assert labels["边军月饷"]["amount"] == pytest.approx(-12.35)
    assert treasury["regular"] == pytest.approx(sum(item["amount"] for item in treasury["regular_detail"]))
    assert round(sum(item["shown"] for item in treasury["regular_detail"]), 1) == treasury["shown"]["regular"]
    for row in rows.values():
        steps = ("edicts", "delayed", "regular", "events", "rounding")
        assert round(row["shown"]["opening"] + sum(row["shown"].get(step, 0) for step in steps), 1) == row["shown"]["closing"]
    assert rows["grain"]["regular_detail"][0]["label"] == "漕运"
    assert "  - 经常收支明细：" in _turn_text(result, show_memorial=False)
    assert "regular_detail" not in result["report"]  # 只经 reconciliation 公开（隐藏资源不列）


# ---- #46 终局季提示与延长模式 ----------------------------------------------------------------------------


def test_final_season_notice_and_overtime_caveat(mech_env, mech_scenario) -> None:
    definition = mech_scenario["scenario"]
    state = build_initial_state(mech_scenario)
    state.turn = 6
    assert final_season_notice(state, definition) is None
    state.turn = 7
    notice = final_season_notice(state, definition)
    assert notice["next_is_final"] and notice["last_turn"] == 8 and "下次结算即终局" in notice["text"] and "延至 T10" in notice["text"]
    state.flags["uprising"] = True
    assert final_season_notice(state, definition) is None  # 加时：时限延至 T10
    state.turn = 9
    assert final_season_notice(state, definition)["last_turn"] == 10 and "延至" not in final_season_notice(state, definition)["text"]

    service = GameService()
    session_id, session = service.create_session("mech_test", 3)
    assert service.view(session_id)["final_notice"] is None
    session.scenario["scenario"]["game_length"]["last_turn"] = 1  # 时限缩到 T1：开局即最后一季
    assert "下次结算即终局" in service.briefing(session_id)
    result = asyncio.run(service.end_turn(session_id, narrate=False))
    assert result["outcome"] is not None and result["final_notice"] is None


def test_extension_hint_is_never_shown_without_an_implemented_mode(mech_scenario) -> None:
    definition = copy.deepcopy(mech_scenario["scenario"])
    for ending in definition["endings"]:
        ending["extension_hint"] = "可进入延长模式。"
    definition["game_length"]["extension_mode"] = "epilogue"
    scenario = {**mech_scenario, "scenario": definition}
    assert validate_scenario(scenario) == []
    state = build_initial_state(scenario)
    outcome = judge_outcome(state, definition, 8)
    assert outcome is not None and "延长模式" not in outcome.text


# ---- #48 召对方式 -------------------------------------------------------------------------------------


def test_audience_mode_in_person_written_and_envoy(mech_env, mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    assert audience_mode(state, "chancellor")["label"] == "面见"
    written = audience_mode(state, "plotter")
    assert written["mode"] == "written" and written["label"] == "书面奏对" and "北镇" in written["note"]
    moving, _ = apply({"op": "move_character", "character": "general", "to_region": "pass"}, state)
    assert audience_mode(moving, "general")["label"] == "遣使问对"
    system = minister_chat_messages("plotter", state, "边事如何？", mech_scenario, 3)[0]["content"]
    assert "召对方式：书面奏对" in system and "不得写成当面应对" in system
    assert offline_minister_text("plotter", state, "边事如何？", mech_scenario, 3).startswith("〔书面奏对·自北镇驿递〕")
    assert not offline_minister_text("chancellor", state, "国用如何？", mech_scenario, 3).startswith("〔")

    service = GameService()
    session_id, _ = service.create_session("mech_test", 3)
    card = service.audience_card(session_id, "plotter")
    assert card["mode"]["label"] == "书面奏对" and "书面奏对" in card["voice"]
    assert service.audience_card(session_id, "chancellor")["mode"]["label"] == "面见"
    reply = asyncio.run(service.chat(session_id, "plotter", "边事如何？"))
    assert reply["mode"]["mode"] == "written" and reply["content"].startswith("〔书面奏对")


def test_rest_chat_status_event_carries_audience_mode(mech_env) -> None:
    from api.app import create_app

    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 3}).json()["id"]
    body = client.post(f"/api/sessions/{session_id}/chat/plotter", json={"text": "边事如何？"}).text
    assert '"mode": "written"' in body and '"mode_label": "书面奏对"' in body


# ---- #49 结构化导出 -----------------------------------------------------------------------------------


def test_export_log_per_season_public_only(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 3)
    ops = [{"op": "set_army_commander", "army": "pass_army", "character": "eunuch"}, {"op": "adjust_resource", "resource": "treasury", "delta": 9}]
    draft = _draft(service, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}], ops=ops)
    service.confirm_edict(session_id, draft["id"])
    asyncio.run(service.end_turn(session_id, narrate=False))
    service.choose(session_id, "choice-petition-1", "grant")
    asyncio.run(service.end_turn(session_id, narrate=False))
    exported = service.export_log(session_id)
    first, second = exported["seasons"][0], exported["seasons"][1]
    assert first["turn"] == 0 and first["adopted"] == [{"memorial_id": "m_tax", "option_id": "approve", "title": "请减赋", "label": "准减赋"}]
    assert first["drafts"][0]["edict_id"] == draft["id"] and first["confirms"][0]["edict_id"] == draft["id"]
    statuses = {row["op"]["op"]: row["status"] for row in first["client_ops"]}
    assert statuses == {"set_army_commander": "applied", "adjust_resource": "rejected"}
    assert first["end_turn"]["resolved_to"] == 1 and first["end_turn"]["reconciliation"] and first["end_turn"]["crisis"]
    assert first["end_turn"]["crisis_change"]["lines"]
    assert any("风闻" in line for line in first["end_turn"]["resources"])
    assert second["choices"] == [{"choice_id": "choice-petition-1", "option_id": "grant", "title": "边将请饷", "option": "准其所请"}]
    for season in exported["seasons"]:
        if season["end_turn"]:
            assert "ambition" not in {row["resource"] for row in season["end_turn"]["reconciliation"]}
    # 隐藏资源真值不出现
    truth = service.session(session_id).state.resources["ambition"].amount
    assert f'"ambition": {truth}' not in json.dumps(exported, ensure_ascii=False)
    assert [row["turn"] for row in service.export_log(session_id, from_turn=1, to_turn=1)["seasons"]] == [1]
    # 拟诏不改日志
    assert all(entry["type"] != "edict" or entry["payload"]["action"] == "confirm" for entry in session.entries)


def test_export_rest_and_mcp(mech_env) -> None:
    from mcp.client.client import Client

    from api.app import create_app

    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 3}).json()["id"]
    client.post(f"/api/sessions/{session_id}/turn")
    response = client.get(f"/api/sessions/{session_id}/export", params={"from_turn": 0, "to_turn": 0})
    assert response.status_code == 200 and response.json()["seasons"][0]["end_turn"]["resolved_to"] == 1
    assert client.get(f"/api/sessions/{session_id}/export", params={"from_turn": 2, "to_turn": 1}).status_code == 422

    async def run() -> dict:
        async with Client(build_server(GameService(), mode="client")) as mcp_client:
            result = await mcp_client.call_tool("export_log", {"session_id": session_id})
            return json.loads("".join(getattr(item, "text", "") for item in result.content))

    assert asyncio.run(run())["session_id"] == session_id


# ---- 条件：army_commander / army_region ---------------------------------------------------------------


def test_army_commander_and_region_conditions(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    assert matches({"kind": "army_commander", "army": "guard", "character": "general"}, state)
    assert matches({"kind": "army_commander", "army": "pass_army", "character": None}, state)
    assert matches({"kind": "army_commander", "army": "guard", "character": "plotter", "operator": "ne"}, state)
    assert matches({"kind": "army_region", "army": "guard", "region": "capital"}, state)
    assert matches({"kind": "army_region", "army": "guard", "region": "pass", "operator": "ne"}, state)
    assert not matches({"kind": "army_region", "army": "ghost", "region": "pass"}, state)
    dead, _ = apply({"op": "set_character_status", "character": "general", "status": "dead"}, state)
    assert matches({"kind": "army_commander", "army": "guard", "character": None}, dead)
    assert describe_condition({"kind": "army_commander", "army": "guard", "character": "general", "operator": "ne"}, state) == "禁军不由郭将军统领"
    assert describe_condition({"kind": "army_region", "army": "guard", "region": "pass"}, state) == "禁军驻雄关"
    assert describe_condition({"kind": "army_commander", "army": "pass_army", "character": None}, state) == "关军无统帅"
    assert condition_errors({"kind": "army_commander", "army": "guard", "character": "x", "operator": "gt"}, "c")
    assert condition_errors({"kind": "army_commander", "army": "guard"}, "c")
    assert condition_errors({"kind": "army_region", "army": "guard", "region": "pass", "operator": "ne"}, "c") == []
