"""第 4 轮（GPT 试玩报告）引擎修复：玩家操作权限、op_reference 过滤、率军出征与统帅行程、谏言人校验、
危机计单一快照、延期原因与季报对账、情报精度、结局条件与终局复盘、离线文本润色。"""

import asyncio
import json
import random
import re

import pytest
from conftest import FIXTURE_SCENARIOS

from simcore.agent.offline import offline_minister_reply
from simcore.agent.roles import interpret_edict
from simcore.agent.tools import PLAYER_TIER, AgentTools
from simcore.domain import reference as reference_module
from simcore.domain.apply import apply
from simcore.domain.describe import describe_condition, describe_satisfied
from simcore.domain.digest import render_digest
from simcore.domain.memorials import summarize_ops, visible_memorials
from simcore.domain.models import Fact, TurnReport
from simcore.domain.permissions import check_player_op, player_ops
from simcore.domain.turn import resolve_turn
from simcore.domain.visibility import intel_precision, public_report
from simcore.llm.mock import MockProvider
from simcore.mcp.server import _turn_text, build_server
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.service import GameError, GameService


def _draft(service: GameService, session_id: str, **kwargs) -> dict:
    return asyncio.run(service.draft_edict(session_id, use_provider=False, **kwargs))


def _codes(receipt: dict) -> list:
    return [clause.get("code") for clause in receipt["clauses"]]


# ---- #22 玩家操作分层 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        {"op": "adjust_resource", "resource": "treasury", "delta": 100000},
        {"op": "adjust_region", "region": "capital", "stat": "defense", "delta": 50},
        {"op": "adjust_relation", "a": "chancellor", "b": "plotter", "delta": 50},
        {"op": "adjust_army", "army": "guard", "delta": 50000},
        {"op": "adjust_trait", "character": "plotter", "trait": "loyalty", "delta": 10},
        {"op": "set_flag", "key": "uprising", "value": False},
        {"op": "adjust_counter", "key": "broke", "delta": -5},
        {"op": "set_counter", "key": "broke", "value": 0},
        {"op": "set_region_controller", "region": "north", "faction": "court"},
        {"op": "set_army_faction", "army": "rebel_army", "faction": "court"},
        {"op": "add_ledger", "name": "天降横财", "category": "treasury", "amount": 999, "kind": "recurring"},
        {"op": "choose_event_option", "choice_id": "x", "option_id": "y"},
    ],
)
def test_forbidden_player_ops_are_rejected_with_op_not_permitted(mech_env, op) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 3)
    before = session.state.model_dump_json()
    receipt = _draft(service, session_id, ops=[op])
    clause = receipt["clauses"][0]
    assert clause["status"] == "rejected" and clause["code"] == "OP_NOT_PERMITTED"
    assert re.search(r"[一-鿿]", clause["reason"]) and op["op"] in clause["reason"]
    service.confirm_edict(session_id, receipt["id"])
    assert service.session(session_id).state.model_dump_json() == before


def test_policy_ops_are_allowed_and_memorials_keep_full_op_set(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 3)
    receipt = _draft(
        service,
        session_id,
        adopted=[{"memorial_id": "m_tax", "option_id": "approve"}],  # 奏疏选项含 adjust_resource、set_flag：剧本层，照常执行
        ops=[
            {"op": "appoint", "character": "general", "office": "marshal"},
            {"op": "set_character_status", "character": "eunuch", "status": "retired", "cause": "年老"},
            {"op": "march", "army": "pass_army", "to_region": "plain"},
            {"op": "remember", "character": "general", "fact": "陛下嘉其忠勤"},
        ],
    )
    assert [clause["status"] for clause in receipt["clauses"]] == ["applied"] * 6
    service.confirm_edict(session_id, receipt["id"])
    state = service.session(session_id).state
    assert state.resources["treasury"].amount == 950 and state.flags["tax_cut"] is True


def test_status_targets_and_foreign_entities_are_limited(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    dead = check_player_op({"op": "set_character_status", "character": "plotter", "status": "dead"}, state)
    assert dead is not None and dead.code == "OP_NOT_PERMITTED" and "已故" in dead.message
    assert check_player_op({"op": "set_character_status", "character": "ruler", "status": "retired"}, state) is not None
    assert check_player_op({"op": "set_character_status", "character": "plotter", "status": "imprisoned"}, state) is None
    enemy = check_player_op({"op": "move_army", "army": "rebel_army", "to_region": "plain"}, state)
    assert enemy is not None and "不归本朝节制" in enemy.message
    state.rules["player_ops"] = {"statuses": ["imprisoned", "dead"], "deny": ["remember"], "allow": ["adjust_region"]}
    assert check_player_op({"op": "set_character_status", "character": "plotter", "status": "dead"}, state) is None
    assert check_player_op({"op": "remember", "character": "general", "fact": "x"}, state) is not None
    assert "adjust_region" in player_ops(state) and "remember" not in player_ops(state)
    state.rules["player_ops"] = ["appoint"]
    assert player_ops(state) == ["appoint"]


def test_projects_standing_orders_and_ledgers_cannot_mint_value(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)

    def project(cost: float, gain: float, turns: int = 2, region: str = "capital") -> dict:
        return {
            "op": "start_project",
            "name": "修城",
            "region": region,
            "cost_per_turn": {"treasury": cost},
            "turns": turns,
            "on_complete": [{"op": "adjust_region", "region": region, "stat": "defense", "delta": gain}],
        }

    assert check_player_op(project(20, 4), state) is None
    assert "正数" in check_player_op(project(-500, 4), state).message
    assert "花费不足" in check_player_op(project(1, 8), state).message
    assert check_player_op(project(500, 40), state) is not None  # 单项增益上限
    assert check_player_op(project(20, 4, region="north"), state) is not None  # 叛军控制之地
    minting = {"op": "start_project", "name": "x", "region": "capital", "cost_per_turn": {"treasury": 50}, "turns": 1,
               "on_complete": [{"op": "adjust_resource", "resource": "treasury", "delta": 9999}]}
    assert check_player_op(minting, state).code == "OP_NOT_PERMITTED"
    order = {"op": "add_standing_order", "text": "岁增国库", "ops": [{"op": "adjust_resource", "resource": "treasury", "delta": 50}]}
    assert "常设令" in check_player_op(order, state).message
    state, _ = apply({"op": "add_ledger", "name": "边费", "category": "treasury", "amount": -30, "kind": "recurring", "cancellable": False}, state)
    locked = check_player_op({"op": "cancel_ledger", "ledger_id": state.ledger[0].id}, state)
    assert locked is not None and "既定开支" in locked.message


def test_llm_interpreted_text_is_player_tier() -> None:
    scenario = load_scenario("../scenarios/_template")
    state = build_initial_state(scenario)
    receipt = asyncio.run(interpret_edict({"finance": "国库增加 10"}, scenario, state, MockProvider(scenario), seed=1))
    assert receipt["clauses"][0]["code"] == "OP_NOT_PERMITTED" and receipt["clauses"][0]["source"] == "text"


def test_player_ops_settings_are_validated(mech_scenario) -> None:
    broken = json.loads(json.dumps(mech_scenario, default=str))
    broken["scenario"]["settings"]["player_ops"] = {"allow": ["mint_money"], "statuses": ["zombie"]}
    errors = validate_scenario(broken)
    assert any("mint_money" in error for error in errors) and any("zombie" in error for error in errors)


# ---- #35 op_reference 过滤 ------------------------------------------------------------------------------


def test_op_reference_is_compact_and_filterable(mech_env, monkeypatch) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 3)
    monkeypatch.setattr(reference_module, "TABLE_LIMIT", 2)
    compact = service.op_reference(session_id)
    names = [row["op"] for row in compact["permitted_ops"]]
    assert "adjust_resource" not in names and "march" in names and "appoint" in names
    assert "adjust_resource" in compact["scenario_only_ops"] and "choose_event_option" in compact["scenario_only_ops"]
    assert all(row["fields"] and row["example"]["op"] == row["op"] for row in compact["permitted_ops"])
    characters = compact["entities"]["characters"]
    assert characters["count"] == 5 and len(characters["rows"]) == 2 and "entity_kind" in characters["more"]
    assert "schema" not in compact
    one = service.op_reference(session_id, op="march")
    assert one["permitted"] and one["schema"]["properties"]["with_commander"]["default"] is True
    forbidden = service.op_reference(session_id, op="adjust_resource")
    assert forbidden["permitted"] is False and "OP_NOT_PERMITTED" in forbidden["note"]
    table = service.op_reference(session_id, entity_kind="characters")
    assert table["count"] == 5 and {row["id"] for row in table["rows"]} >= {"plotter", "general"}
    picked = service.op_reference(session_id, ids=["guard", "plotter", "ghost"])
    assert set(picked["entities"]) == {"armies", "characters"} and picked["missing"] == ["ghost"]
    with pytest.raises(GameError) as unknown:
        service.op_reference(session_id, op="mint")
    assert unknown.value.code == "UNKNOWN_OP"

    async def via_mcp() -> str:
        from mcp import Client

        async with Client(build_server(service)) as client:
            result = await client.call_tool("op_reference", {"session_id": session_id, "op": "set_army_commander"})
            return result.content[0].text

    assert '"permitted": true' in asyncio.run(via_mcp())


# ---- #27 率军出征、统帅随军、赴任 ------------------------------------------------------------------------


def test_march_moves_army_and_commander_as_one_transit(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state, facts = apply({"op": "march", "army": "guard", "to_region": "plain"}, state)
    army, general = state.armies["guard"], state.characters["general"]
    assert army.in_transit == general.in_transit and army.in_transit["eta_turn"] == 2
    assert "郭将军随军" in facts[0].text and "出征" in facts[0].text
    digest = render_digest(state, 3, None, None, mech_scenario)
    assert "郭将军（自京畿赴平原，约2季后到）" in digest
    for _ in range(2):
        state, _ = resolve_turn(state, random.Random(1), mech_scenario)
    assert state.armies["guard"].region == "plain" and state.characters["general"].region == "plain"


def test_move_army_without_commander_and_remote_appointment(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state, _ = apply({"op": "move_army", "army": "guard", "to_region": "pass", "with_commander": False}, state)
    assert state.characters["general"].in_transit is None
    # 剧本层任帅（缺省遥领）：人不动，事实写明副将暂摄
    remote, facts = apply({"op": "set_army_commander", "army": "pass_army", "character": "eunuch"}, state)
    assert remote.characters["eunuch"].in_transit is None and "副将暂摄" in facts[0].text
    # 玩家层任帅：自动赴任
    tools = AgentTools(state)
    receipt = tools.propose_ops([{"op": "set_army_commander", "army": "pass_army", "character": "eunuch"}], tier=PLAYER_TIER)
    assert receipt[0]["status"] == "applied" and receipt[0]["op"]["dispatch"] is True
    traveller = tools.state.characters["eunuch"]
    assert traveller.in_transit and traveller.in_transit["to_region"] == "pass"
    again = tools.propose_ops([{"op": "move_character", "character": "eunuch", "to_region": "pass"}], tier=PLAYER_TIER)
    assert again[0]["code"] == "INVALID_TARGET" and "无须再下令" in again[0]["reason"]


def test_battle_logs_acting_commander_when_commander_is_away(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.armies["pass_army"].commander = "plotter"  # 统帅远在北镇
    state.armies["rebel_army"].region = "pass"
    state, report = resolve_turn(state, random.Random(2), mech_scenario)
    assert report.battles and report.battles[0]["commanders"]["pass_army"]["present"] is False
    assert any(fact.kind == "command" and "副将暂摄" in fact.text for fact in report.facts)


# ---- #25 谏言人校验 ------------------------------------------------------------------------------------


def test_absent_advisors_are_dropped_and_requires_is_checked(mech_scenario) -> None:
    scenario = json.loads(json.dumps(mech_scenario, default=str))
    scenario["memorials"] = [{
        "id": "m_x",
        "title": "议事",
        "options": [{"id": "ok", "label": "可", "ops": []}],
        "advisors": [
            {"character": "chancellor", "stance": "support", "line": "宰相之言"},
            {"character": "plotter", "stance": "oppose", "line": "北镇之言", "requires": {"region": "north"}},
            {"character": "eunuch", "stance": "neutral", "line": "内侍之言", "requires": {"office": "censor"}},
            {"character": "general", "stance": "neutral", "line": "旧日之言", "requires": {"alive": False}},
        ],
    }]
    assert validate_scenario(scenario) == []
    state = build_initial_state(scenario)
    state, _ = apply({"op": "set_character_status", "character": "chancellor", "status": "dead"}, state)
    state, _ = apply({"op": "set_character_status", "character": "general", "status": "exiled"}, state)
    advisors = visible_memorials(scenario, state)[0]["advisors"]
    assert [row["character"] for row in advisors] == ["plotter", "general"]
    assert advisors[1]["name"] == "郭将军（旧议）" and advisors[1]["old"] is True
    state, _ = apply({"op": "move_character", "character": "plotter", "to_region": "plain"}, state)
    assert [row["character"] for row in visible_memorials(scenario, state)[0]["advisors"]] == ["general"]
    scenario["memorials"][0]["advisors"][1]["requires"] = {"realm": "x"}
    assert any("未知键" in error for error in validate_scenario(scenario))


# ---- #30 危机计单一快照 ---------------------------------------------------------------------------------


def test_crisis_numbers_are_identical_across_end_turn_report_view_and_briefing(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 5)
    for _ in range(3):
        result = asyncio.run(service.end_turn(session_id, narrate=False))
        view = service.view(session_id)
        assert result["report"]["crisis"] == view["crisis"] == view["last_report"]["crisis"]
        briefing = service.briefing(session_id)
        assert f"危机计：{view['crisis']['title']} {view['crisis']['display']}" in briefing
        for term in view["crisis"]["terms"]:
            assert f"- 分项 {term['label']}：{term['display']}" in briefing
            assert f"- {term['label']}：{term['display']}" in _turn_text(result, show_memorial=False)


# ---- #32 延期原因与季报对账 -----------------------------------------------------------------------------


def test_delayed_clauses_carry_reason_and_due_turn(mech_scenario) -> None:
    scenario = json.loads(json.dumps(mech_scenario, default=str))
    scenario["scenario"]["formulas"]["execution"] = {"base_delay": 0.2, "corruption_factor": 0.02}
    state = build_initial_state(scenario)
    receipt = asyncio.run(interpret_edict({}, scenario, state, None, seed=1, client_ops=[{"op": "appoint", "character": "general", "office": "marshal"}]))
    clause = receipt["clauses"][0]
    assert clause["status"] == "delayed" and clause["due_turn"] == 1
    assert "积弊高（40），有司稽迟" in clause["delay_reason"] and "延期概率约 100%" in clause["delay_reason"]


def test_season_reconciliation_sums_exactly_with_fixed_seed(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 11)
    receipt = _draft(service, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}])
    service.confirm_edict(session_id, receipt["id"])
    session.append_pending([{"op": "adjust_resource", "resource": "treasury", "delta": -7, "reason": "迟到的赏赐"}], 1, label="诏令")
    first = asyncio.run(service.end_turn(session_id, narrate=False))
    service.choose(session_id, "choice-petition-1", "grant")  # 季前抉择：国库 −100，计入「事件/选择」
    second = asyncio.run(service.end_turn(session_id, narrate=False))
    rows = {row["resource"]: row for row in first["report"]["reconciliation"]}
    treasury = rows["treasury"]
    assert treasury["opening"] == 1000 and treasury["edicts"] == pytest.approx(-50) and treasury["delayed"] == pytest.approx(-7)
    later = {row["resource"]: row for row in second["report"]["reconciliation"]}["treasury"]
    assert later["opening"] == pytest.approx(treasury["closing"]) and later["events"] == pytest.approx(-100)
    for report in (first["report"], second["report"]):
        assert "ambition" not in {row["resource"] for row in report["reconciliation"]}  # 隐藏资源不入对账
        for row in report["reconciliation"]:
            steps = ("edicts", "delayed", "regular", "events")
            assert row["opening"] + sum(row[step] for step in steps) == pytest.approx(row["closing"], abs=1e-6)
            shown = row["shown"]
            assert round(shown["opening"] + sum(shown[step] for step in steps), 1) == shown["closing"]
    assert later["closing"] == pytest.approx(service.session(session_id).state.resources["treasury"].amount)
    text = _turn_text(first, show_memorial=False)
    assert "## 对账（期初 → 诏令 → 延期执行 → 经常收支 → 事件/选择 → 期末）" in text
    assert "国库（万贯）：期初 1000 → 诏令 −50 → 延期执行 −7" in text


# ---- #29 情报精度 --------------------------------------------------------------------------------------


def test_intel_precision_sources_and_stacking_cap(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    base = intel_precision(state, "ambition")
    assert base["narrowing"] == 0 and base["display"].startswith("±") and base["sources"] == []
    state, _ = apply({"op": "set_flag", "key": "intel:ambition", "value": True}, state, source="密结边将")
    once = intel_precision(state, "ambition")
    assert once["half_width"] == pytest.approx(base["half_width"] / 2) and once["sources"] == ["密结边将"] and not once["capped"]
    state, _ = apply({"op": "set_flag", "key": "intel:ambition", "value": True}, state, source="中使实奏")
    twice = intel_precision(state, "ambition")
    assert twice["sources"] == ["密结边将", "中使实奏"] and twice["capped"] and twice["half_width"] == once["half_width"]
    state.rules["intel_min_width"] = 1
    stacked = intel_precision(state, "ambition")
    assert stacked["half_width"] == pytest.approx(base["half_width"] / 4) and not stacked["capped"]


def test_briefing_and_view_state_precision(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 4)
    view = service.view(session_id)
    assert view["state"]["resources"]["ambition"]["precision"]["display"].startswith("±")
    assert "精度 ±" in service.briefing(session_id)


# ---- #31 结局条件与终局复盘 ---------------------------------------------------------------------------------


def test_condition_rendering_in_chinese(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    assert describe_condition({"kind": "resource", "id": "risk", "operator": "lt", "value": 60}, state) == "危机 < 60"
    assert describe_condition({"not": {"kind": "region_controller", "region": "capital", "faction": "rebels"}}, state) == "京畿不归叛军"
    state.rules["flag_labels"] = {"uprising": "范阳起兵"}
    assert describe_condition({"not": {"kind": "flag", "key": "uprising", "value": True}}, state) == "「范阳起兵」未成"
    either = {"any": [{"kind": "flag", "key": "uprising"}, {"kind": "resource", "id": "risk", "operator": "lt", "value": 60}]}
    lines = describe_satisfied({"all": [{"kind": "turn", "operator": "gte", "value": 0}, either]}, state, 0)
    assert lines == ["回合 ≥ T0", "危机 < 60"]


def test_outcome_lists_conditions_and_review_replaces_memorials(mech_env) -> None:
    service = GameService()
    session_id, session = service.create_session("mech_test", 1)
    session.append_op({"op": "set_region_controller", "region": "capital", "faction": "rebels"})
    result = asyncio.run(service.end_turn(session_id, narrate=False))
    assert result["outcome"]["satisfied"] == ["T1 时京畿归叛军"]
    assert "判定条件：「T1 时京畿归叛军」" in _turn_text(result, show_memorial=False)
    view = service.view(session_id)
    assert view["memorials"] == [] and view["pending_choices"] == []
    review = view["review"]
    assert review["satisfied"] == ["T1 时京畿归叛军"] and review["objectives"] and review["stats"]
    assert all(line.startswith("T") for line in review["highlights"])
    briefing = service.briefing(session_id)
    assert "## 终局复盘" in briefing and "## 本季奏疏" not in briefing and "## 待决事件" not in briefing


def test_time_up_outcome_explains_deadline(mech_scenario) -> None:
    from simcore.domain.turn import judge_outcome

    state = build_initial_state(mech_scenario)
    outcome = judge_outcome(state, mech_scenario["scenario"], 8)
    assert outcome is not None and outcome.id == "open_storm" and "终局时限 T8" in outcome.satisfied[0]


# ---- 离线文本润色 --------------------------------------------------------------------------------------


def test_symmetric_relation_facts_are_deduped(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    ops = [
        {"op": "adjust_relation", "a": "chancellor", "b": "plotter", "delta": -10},
        {"op": "adjust_relation", "a": "plotter", "b": "chancellor", "delta": -10},
    ]
    assert summarize_ops(ops, state) == ["李相↔安节度 关系 −10"]
    facts: list[Fact] = []
    for op in ops:
        state, produced = apply(op, state, source="相争")
        facts.extend(produced)
    public = public_report(TurnReport(turn=0, facts=facts), state, 1) or {}
    assert len([fact for fact in public["facts"] if fact["kind"] == "relation"]) == 1


def test_offline_audience_paraphrases_digest_rows_and_does_not_repeat_closings(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    for seed in range(12):
        digest = render_digest(state, seed, "eunuch", None, mech_scenario)
        text = offline_minister_reply("eunuch", state, "禁军如何？", mech_scenario, seed, digest)
        assert "禁军20000" not in text and "驻京畿，属朝廷" not in text
        sentences = [piece for piece in re.split(r"(?<=[。！？])", text) if piece]
        assert len(sentences) == len(set(sentences))
    digest = render_digest(state, 1, "eunuch", None, mech_scenario)
    text = offline_minister_reply("eunuch", state, "禁军如何？", mech_scenario, 1, digest)
    assert "禁军二万" in text and "皆在郭将军麾下" in text


def test_fixture_scenarios_still_validate() -> None:
    assert validate_scenario(load_scenario(FIXTURE_SCENARIOS / "mech_test")) == []
