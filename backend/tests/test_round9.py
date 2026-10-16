"""第 9 轮（CONTRACT §7）：朝会日程、遣使问对、廷议在场与书面议状、驿传（奏到 / 令达）、政令卡、军令姿态与将领自主、
兵部总览、新手引导。测试场景 r9_test：京畿为京城；西陲（行程 3 季 → 驿程 70 日）、东海（courier_days 90）为远地。"""

import asyncio
import json
import random
import re

import pytest
from conftest import FIXTURE_SCENARIOS
from fastapi.testclient import TestClient
from mcp import Client

from api.app import create_app
from simcore.agent.roles import interpret_edict
from simcore.domain.battle import resolve_battles
from simcore.domain.cards import card_errors, cards_view, expand_card
from simcore.domain.courier import courier_days, is_far
from simcore.domain.memorials import visible_memorials
from simcore.domain.military import autonomy_preview, autonomy_probability, roll_autonomy, scorch_effects
from simcore.domain.models import Army, Region, clone
from simcore.domain.permissions import check_player_op
from simcore.domain.visibility import battle_breakdown, public_view
from simcore.llm.mock import MockProvider
from simcore.mcp.server import build_server
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.service import GameError, GameService
from simcore.store.session import state_hash

WARS = [["court", "rebels"]]


@pytest.fixture
def r9_scenario() -> dict:
    return load_scenario(FIXTURE_SCENARIOS / "r9_test")


@pytest.fixture
def r9_state(r9_scenario):
    return build_initial_state(r9_scenario)


def new_game(seed: int = 1) -> tuple[GameService, str]:
    service = GameService()
    session_id, _ = service.create_session("r9_test", seed)
    return service, session_id


def end_turn(service: GameService, session_id: str) -> dict:
    return asyncio.run(service.end_turn(session_id, narrate=False))


# ---------------------------------------------------------------- 7.1 朝会日程与遣使问对


def test_agenda_limits_audiences_and_resets_each_season(mech_env) -> None:
    service, session_id = new_game()
    view = service.view(session_id)
    assert view["agenda"]["audience"] == {"used": 0, "limit": 2, "left": 2, "label": "召对"}
    assert view["agenda"]["text"] == "召对 0/2、廷议 0/1"
    first = service.begin_audience(session_id, "chancellor", "国用如何？")
    assert first["deferred"] is False and first["mode"]["mode"] == "in_person"
    service.begin_audience(session_id, "general")
    with pytest.raises(GameError) as exc:
        service.begin_audience(session_id, "eunuch")
    assert exc.value.code == "AGENDA_EXHAUSTED" and exc.value.status == 409 and "2/2" in exc.value.message
    state = service.session(session_id).head_state()
    assert state.counters["_agenda:audience"] == 2  # 计数在状态里（进入哈希）
    assert "朝会日程（本季）：召对 2/2、廷议 0/1" in service.briefing(session_id)
    end_turn(service, session_id)
    after = service.session(session_id).head_state()
    assert "_agenda:audience" not in after.counters
    assert service.view(session_id)["agenda"]["audience"]["used"] == 0


def test_unconfigured_scenario_keeps_old_audience_behaviour(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    for _ in range(5):
        started = service.begin_audience(session_id, "plotter")  # 远地，但剧本未配置 agenda：当场书面作答，不计数
        assert started["deferred"] is False
    state = service.session(session_id).head_state()
    assert not any(key.startswith("_agenda:") for key in state.counters)
    assert service.view(session_id)["agenda"]["text"] == "召对 0/不限、廷议 0/不限"


def test_rest_remote_audience_is_answered_next_season(mech_env) -> None:
    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "r9_test", "seed": 2}).json()["id"]
    response = client.post(f"/api/sessions/{session_id}/chat/westlord", json={"text": "边情如何？"})
    assert response.status_code == 200
    assert "event: deferred" in response.text and "使者已发，回奏于下季送达" in response.text
    assert client.get(f"/api/sessions/{session_id}/view").json()["replies"] == []
    turned = client.post(f"/api/sessions/{session_id}/turn").json()
    assert turned["replies"][0]["character"] == "westlord"
    replies = client.get(f"/api/sessions/{session_id}/view").json()["replies"]
    assert len(replies) == 1
    reply = replies[0]
    assert reply["name"] == "西帅" and reply["question"] == "边情如何？" and reply["asked_turn"] == 0 and reply["turn"] == 1
    assert reply["text"].startswith("〔书面奏对·自西陲驿递〕") and reply["roleplay"] is False
    # 朝会已满 → 流开始之前 409
    client.post(f"/api/sessions/{session_id}/chat/chancellor", json={"text": "一"})
    client.post(f"/api/sessions/{session_id}/chat/general", json={"text": "二"})
    blocked = client.post(f"/api/sessions/{session_id}/chat/eunuch", json={"text": "三"})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "AGENDA_EXHAUSTED"


# ---------------------------------------------------------------- 7.2 廷议在场与书面议状


def test_debate_requires_presence_and_written_opinions_arrive_next_season(mech_env) -> None:
    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "r9_test", "seed": 3}).json()["id"]
    present = client.get(f"/api/sessions/{session_id}/present").json()
    assert present["capital"] == {"id": "capital", "name": "京畿"}
    assert {row["character"] for row in present["present"]} == {"chancellor", "general", "eunuch"}
    absent = {row["character"]: row for row in present["absent"]}
    assert absent["westlord"]["whereabouts"] == "在西陲" and absent["plotter"]["whereabouts"] == "在北镇"
    refused = client.post(f"/api/sessions/{session_id}/debate", json={"topic": "边镇增兵议", "participants": ["chancellor", "westlord"]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "NOT_PRESENT"
    assert "西陲" in refused.json()["detail"]["message"] and "written" in refused.json()["detail"]["message"]
    wrong = client.post(f"/api/sessions/{session_id}/debate", json={"topic": "边镇增兵议", "participants": [], "written": ["general"]})
    assert wrong.status_code == 422
    debate = client.post(
        f"/api/sessions/{session_id}/debate", json={"topic": "边镇增兵议", "participants": ["chancellor"], "written": ["westlord"]}
    ).json()
    assert [row["character"] for row in debate["speeches"]] == ["chancellor"]
    assert debate["written"] == [{"character": "westlord", "name": "西帅", "whereabouts": "西陲", "note": "书面议状已发，其议于下季送达（入谏言库）"}]
    assert "stance" not in json.dumps(debate["written"])  # 立场与理由下季才送达
    again = client.post(f"/api/sessions/{session_id}/debate", json={"topic": "再议", "participants": ["general"]})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "AGENDA_EXHAUSTED"
    assert [row for row in client.get(f"/api/sessions/{session_id}/advice").json() if row["source"] == "书面议状"] == []
    client.post(f"/api/sessions/{session_id}/turn")
    written = [row for row in client.get(f"/api/sessions/{session_id}/advice").json() if row["source"] == "书面议状"]
    assert len(written) == 1 and written[0]["character"] == "westlord" and written[0]["turn"] == 1
    assert written[0]["text"].startswith("〔书面议状·自西陲驿递〕就「边镇增兵议」")
    assert client.get(f"/api/sessions/{session_id}/view").json()["written_opinions"][0]["character"] == "westlord"


# ---------------------------------------------------------------- 7.3 驿传


def test_courier_days_are_written_or_derived(r9_state) -> None:
    assert courier_days(r9_state, "capital") == 3
    assert courier_days(r9_state, "pass") == 15
    assert courier_days(r9_state, "plain") == 40
    assert courier_days(r9_state, "west") == 70  # 行程 3 季
    assert courier_days(r9_state, "east") == 90  # 剧本写明
    assert is_far(r9_state, "west") and is_far(r9_state, "east") and not is_far(r9_state, "plain")
    # 新字段未写时不进入序列化：旧存档状态哈希不变
    assert "courier_days" not in r9_state.regions["west"].model_dump(mode="json")
    assert "posture" not in r9_state.armies["guard"].model_dump(mode="json")
    assert Region(id="r", name="r").model_dump() == {k: v for k, v in Region(id="r", name="r").model_dump().items() if k != "courier_days"}
    assert "posture" not in Army(id="a", name="a", region="r").model_dump(mode="json")


def test_far_memorials_arrive_a_season_late_with_dates(mech_env) -> None:
    service, session_id = new_game(4)
    view = service.view(session_id)
    ids = [row["id"] for row in view["memorials"]]
    assert "m_tax" in ids and "m_west" not in ids
    tax = next(row for row in view["memorials"] if row["id"] == "m_tax")
    # 京中之奏写「奏上」，不写「自京畿奏到」（《宣和》界面审读）
    assert tax["arrival"] == "正月初四日奏上" and tax["arrived_late"] is False and tax["courier_days"] == 3
    assert "正月初四日奏上" in service.briefing(session_id)
    session = service.session(session_id)
    session.append_op({"op": "set_flag", "key": "force_west", "value": True})
    end_turn(service, session_id)
    view = service.view(session_id)
    west = next(row for row in view["memorials"] if row["id"] == "m_west")
    assert west["arrived_late"] is True and west["origin_turn"] == 0 and west["arrival"].endswith("自西陲奏到")
    assert "远地晚到一季" in service.briefing(session_id)
    # 远地事件：状态即时改变，军报晚一季
    state = session.head_state()
    assert state.regions["west"].stats["prosperity"] == 25
    assert "west_raid" not in [row["id"] for row in view["last_report"]["events"]]
    assert not any(key.startswith("_internal:") for key in view["state"]["flags"])
    # 条件奏疏：本季条件成立，下季才奏到
    session.append_op({"op": "set_flag", "key": "west_alarm", "value": True})
    assert "m_west_alarm" not in [row["id"] for row in service.view(session_id)["memorials"]]
    report = end_turn(service, session_id)["report"]
    late = [row for row in report["events"] if row["id"] == "west_raid"]
    assert late and late[0]["arrived_late"] is True and late[0]["origin_turn"] == 1
    event_fact = next(fact for fact in report["facts"] if fact.get("kind") == "event" and fact.get("entity") == "west_raid")
    assert event_fact["arrived_late"] is True and "军报晚到" in event_fact["text"]
    assert "m_west_alarm" in [row["id"] for row in service.view(session_id)["memorials"]]


def test_orders_to_far_regions_reach_next_season(mech_env) -> None:
    service, session_id = new_game(5)
    receipt = asyncio.run(service.draft_edict(
        session_id,
        ops=[{"op": "appoint", "character": "westlord", "office": "censor"}, {"op": "appoint", "character": "eunuch", "office": "marshal"}],
    ))
    far, near = receipt["clauses"]
    assert far["status"] == "delayed" and far["delay_kind"] == "courier" and "驿程遥远，令达于下季" in far["delay_reason"]
    assert "西陲去京城驿程约 70 日" in far["delay_reason"] and far["due_turn"] == 1
    assert near["status"] == "applied"
    confirmed = service.confirm_edict(session_id, receipt["id"])
    assert confirmed["state"]["offices"]["censor"]["holder"] is None
    end_turn(service, session_id)
    assert service.session(session_id).head_state().offices["censor"].holder == "westlord"


# ---------------------------------------------------------------- 7.4 政令卡


def test_policy_cards_view_filters_options(mech_env) -> None:
    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "r9_test", "seed": 6}).json()["id"]
    cards = {row["id"]: row for row in client.get(f"/api/sessions/{session_id}/policy_cards").json()}
    assert set(cards) == {"card_march", "card_hold", "card_appoint", "card_wall"}
    appoint = cards["card_appoint"]
    who = {row["id"] for row in appoint["params"][0]["options"]}
    assert who == {"chancellor", "general", "eunuch"}  # 在京、未达兼职上限；不列君主、远地之人
    assert {row["id"] for row in appoint["params"][1]["options"]} == {"marshal", "censor"}
    assert appoint["available"] and appoint["cooldown"] == 2 and appoint["category"] == "人事"
    assert {row["id"] for row in cards["card_wall"]["params"][0]["options"]} == {"capital", "pass", "plain", "west", "east"}
    assert cards["card_wall"]["params"][1] == {"name": "cost", "kind": "amount", "label": "每季花费", "min": 20, "max": 100, "default": None}
    assert "rebel_army" not in {row["id"] for row in cards["card_hold"]["params"][0]["options"]}
    view = client.get(f"/api/sessions/{session_id}/view").json()
    assert view["provider_mode"] == "offline"
    draft = client.post(
        f"/api/sessions/{session_id}/edicts/draft",
        json={"cards": [{"card_id": "card_hold", "params": {"army": "pass_army"}}], "sections": {"military": "诸军严守"}},
    ).json()
    assert [(row["source"], row["status"]) for row in draft["clauses"]] == [("card", "applied"), ("text", "narrative_only")]
    assert draft["clauses"][0]["card_id"] == "card_hold" and draft["clauses"][0]["cost_hint"] == "不出战"
    bad = client.post(f"/api/sessions/{session_id}/edicts/draft", json={"cards": [{"params": {}}]})
    assert bad.status_code == 422


def test_card_edict_expands_validates_and_cools_down(mech_env) -> None:
    service, session_id = new_game(7)
    receipt = asyncio.run(service.draft_edict(
        session_id,
        cards=[
            {"card_id": "card_appoint", "params": {"who": "eunuch", "office": "censor"}},
            {"card_id": "card_appoint", "params": {"who": "general", "office": "marshal"}},
            {"card_id": "card_wall", "params": {"region": "capital", "cost": 50}},
            {"card_id": "card_wall", "params": {"region": "capital", "cost": 500}},
            {"card_id": "card_appoint", "params": {"who": "westlord", "office": "censor"}},
            {"card_id": "nope", "params": {}},
        ],
    ))
    clauses = receipt["clauses"]
    assert [row["source"] for row in clauses] == ["card"] * len(clauses)
    assert clauses[0]["status"] == "applied" and clauses[0]["card_title"] == "拜官" and clauses[0]["text"] == "拜官：人选：高内侍，官职：御史"
    assert clauses[1]["op"] == {"op": "remember", "character": "eunuch", "fact": "拜御史"}
    assert clauses[2]["code"] == "CARD_COOLDOWN"  # 有冷却的卡一道诏书只能用一次
    assert clauses[3]["status"] == "applied" and clauses[3]["op"]["cost_per_turn"] == {"treasury": 50}
    assert clauses[4]["code"] == "INVALID_CARD_PARAM" and "20" in clauses[4]["reason"]
    assert clauses[5]["code"] == "INVALID_CARD_PARAM" and "eunuch" in clauses[5]["reason"]
    assert clauses[6]["code"] == "UNKNOWN_CARD"
    service.confirm_edict(session_id, receipt["id"])
    state = service.session(session_id).head_state()
    assert state.offices["censor"].holder == "eunuch" and state.flags["card:card_appoint"] == 0
    cards = {row["id"]: row for row in service.policy_cards(session_id)}
    assert cards["card_appoint"]["available"] is False and cards["card_appoint"]["cooldown_left"] == 2
    end_turn(service, session_id)
    assert {row["id"]: row for row in service.policy_cards(session_id)}["card_appoint"]["cooldown_left"] == 1
    end_turn(service, session_id)
    assert {row["id"]: row for row in service.policy_cards(session_id)}["card_appoint"]["available"] is True


def test_card_requires_and_player_tier(r9_scenario, r9_state) -> None:
    state = clone(r9_state)
    state.resources["treasury"].amount = 50
    card = next(row for row in cards_view(state, r9_scenario) if row["id"] == "card_wall")
    assert card["available"] is False and "条件未备" in card["unavailable_reason"] and "国库 ≥ 100" in card["unavailable_reason"]
    _, ops, summary = expand_card(r9_state, r9_scenario, "card_hold", {"army": "guard"})
    assert ops == [{"op": "set_army_posture", "army": "guard", "posture": "hold"}] and summary == "军队：禁军"


def test_card_validation_errors(r9_scenario) -> None:
    broken = json.loads(json.dumps(r9_scenario, default=str))
    broken["policy_cards"] = [
        {"id": "a", "category": "军务", "title": "甲", "params": [], "ops": [{"op": "adjust_resource", "resource": "treasury", "delta": 5}]},
        {"id": "a", "category": "杂务", "title": "乙", "params": [{"name": "x", "kind": "army"}], "ops": [{"op": "march", "army": "{y}", "to_region": "pass"}]},
        {"id": "c", "category": "人事", "title": "丙", "params": [{"name": "who", "kind": "character", "filter": {"min_trait": {"ambition": 5}}}],
         "ops": [{"op": "remember", "character": "{who}", "fact": "x"}]},
    ]
    errors = card_errors(broken)
    assert any("非玩家级操作：adjust_resource" in row for row in errors)
    assert any("id 重复" in row for row in errors) and any("category" in row for row in errors)
    assert any("未定义的参数：{y}" in row for row in errors)
    assert any("隐藏特质" in row for row in errors)
    assert validate_scenario(FIXTURE_SCENARIOS / "r9_test") == []


def test_offline_free_text_is_narrative_only(r9_scenario, r9_state) -> None:
    receipt = asyncio.run(interpret_edict({"military": "命禁军出征"}, r9_scenario, r9_state, MockProvider(r9_scenario), seed=1))
    clause = receipt["clauses"][0]
    assert clause["status"] == "narrative_only" and clause["reason"] == "未接模型：御笔只记入史册，不生效力；请用奏疏批答或政令卡"


# ---------------------------------------------------------------- 7.5 军令姿态与将领自主


def _battle_state(r9_state, defender_posture=None, attacker_posture=None):
    state = clone(r9_state)
    state.armies["guard"].region = "plain"
    state.armies["guard"].strength = 10000
    state.armies["guard"].posture = defender_posture
    state.armies["rebel_army"].region = "plain"
    state.armies["rebel_army"].strength = 10000
    state.armies["rebel_army"].posture = attacker_posture
    state.rules["wars"] = WARS
    return state


def test_posture_op_is_player_tier_and_changes_battle(r9_state) -> None:
    assert check_player_op({"op": "set_army_posture", "army": "guard", "posture": "hold"}, r9_state) is None
    denied = check_player_op({"op": "set_army_posture", "army": "rebel_army", "posture": "hold"}, r9_state)
    assert denied is not None and denied.code == "OP_NOT_PERMITTED"
    plain = _battle_state(r9_state)
    held = _battle_state(r9_state, defender_posture="hold")
    [base], _ = resolve_battles(plain, random.Random(9), WARS)
    [hold], _ = resolve_battles(held, random.Random(9), WARS)
    assert hold["defender_power"] == pytest.approx(base["defender_power"] * 1.2, rel=1e-3)
    assert hold["attacker_power"] == pytest.approx(base["attacker_power"])
    assert "坚守+20%" in battle_breakdown(hold) and "×军令" in battle_breakdown(hold)
    strong = _battle_state(r9_state, defender_posture="hold")
    strong.armies["guard"].strength = 40000
    [won], _ = resolve_battles(strong, random.Random(9), WARS)
    assert won["winner"] == "court" and won["losses"]["rebel_army"] == pytest.approx(10000 * 0.2)  # 坚守得胜不追击：败方只折二成


def test_attack_posture_loses_more_and_hold_attackers_do_not_engage(r9_state) -> None:
    state = _battle_state(r9_state, attacker_posture="attack")
    state.armies["guard"].strength = 40000  # 守方必胜
    [battle], _ = resolve_battles(state, random.Random(3), WARS)
    assert battle["winner"] == "court" and battle["losses"]["rebel_army"] == pytest.approx(10000 * 0.4)
    assert any("进击+10%" in note for row in battle["factors"]["attacker"] for note in row.get("modifiers", []))
    standoff = clone(r9_state)
    standoff.rules["wars"] = WARS
    standoff.armies["guard"].region = "north"  # 北镇归叛军：禁军为攻方
    standoff.armies["guard"].posture = "hold"
    battles, facts = resolve_battles(standoff, random.Random(3), WARS)
    assert battles == [] and any(fact.kind == "standoff" and "奉令坚守" in fact.text for fact in facts)


def test_scorched_earth_hurts_region_and_enemy_supply(r9_state) -> None:
    state = _battle_state(r9_state, defender_posture="scorch")
    facts = scorch_effects(state, WARS)
    assert state.regions["plain"].stats["prosperity"] == 35 and "坚壁清野" in facts[0].text
    [battle], _ = resolve_battles(clone(state), random.Random(9), WARS)
    [base], _ = resolve_battles(_battle_state(r9_state), random.Random(9), WARS)
    assert battle["attacker_power"] == pytest.approx(base["attacker_power"] * 0.85, rel=1e-3)


def test_autonomy_risk_preview_and_roll(r9_state) -> None:
    march_west = {"op": "march", "army": "west_army", "to_region": "capital"}
    assert autonomy_probability(r9_state, march_west) == pytest.approx(0.5)
    assert autonomy_preview(r9_state, march_west)["level"] == "高"
    assert autonomy_preview(r9_state, {"op": "march", "army": "guard", "to_region": "pass"})["level"] == "低"
    assert autonomy_preview(r9_state, {"op": "march", "army": "pass_army", "to_region": "plain"}) is None  # 无统帅
    state = clone(r9_state)
    state.rules["autonomy"] = {"formula": "1", "outcomes": {"sortie": 1}}
    state.rules["wars"] = WARS
    state.armies["guard"].region = "plain"
    roll = roll_autonomy(state, {"op": "set_army_posture", "army": "guard", "posture": "hold"}, seed=1)
    assert roll["result"] == "sortie" and roll["ops"][1] == {"op": "march", "army": "guard", "to_region": "north"}
    assert "擅自出战" in roll["label"]
    assert roll_autonomy(r9_state, {"op": "march", "army": "guard", "to_region": "pass"}, seed=1) is None


def test_autonomy_hits_at_confirm_and_becomes_a_fact(mech_env) -> None:
    service = GameService()
    scenario = service.scenario("r9_test")
    scenario["scenario"]["settings"]["autonomy"] = {"formula": "1", "outcomes": {"alter": 1}}
    session_id, _ = service.create_session("r9_test", 8)
    receipt = asyncio.run(service.draft_edict(session_id, cards=[{"card_id": "card_march", "params": {"army": "guard", "to": "pass"}}]))
    clause = receipt["clauses"][0]
    assert clause["status"] == "applied" and clause["autonomy_risk"] == "高" and "将领自主风险：高" in clause["autonomy_note"]
    confirmed = service.confirm_edict(session_id, receipt["id"])
    clause = confirmed["clauses"][0]
    assert clause["status"] == "delayed" and clause["delay_kind"] == "autonomy" and "将领自主" in clause["delay_reason"]
    assert confirmed["state"]["armies"]["guard"]["in_transit"] is None
    report = end_turn(service, session_id)["report"]
    assert any(fact["kind"] == "autonomy" for fact in report["facts"])
    assert any(fact["kind"] == "army_posture" and "坚守" in fact["text"] for fact in report["facts"])
    assert service.session(session_id).head_state().armies["guard"].posture == "hold"


def test_armies_overview_has_orders_etas_and_no_hidden_values(mech_env) -> None:
    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "r9_test", "seed": 9}).json()["id"]
    data = client.get(f"/api/sessions/{session_id}/armies").json()
    armies = {row["id"]: row for row in data["armies"]}
    assert set(armies) == {"guard", "pass_army", "west_army"}  # 只列本方
    guard = armies["guard"]
    assert guard["commander"] == {"id": "general", "name": "郭将军", "present": True, "note": guard["commander"]["note"]}
    assert guard["etas"]["capital"] == 0 and guard["etas"]["pass"] == 1 and guard["etas"]["west"] == 3 and guard["etas"]["north"] == 3
    assert guard["posture_label"] == "常态" and "set_army_posture" in guard["orders"] and "card_march" in guard["cards"]
    assert guard["autonomy_risk"] == "低" and armies["west_army"]["autonomy_risk"] == "高" and armies["pass_army"]["autonomy_risk"] is None
    text = json.dumps(data, ensure_ascii=False)
    assert "ambition" not in text and "loyalty" not in text and "0.5" not in text


# ---------------------------------------------------------------- 7.6 新手引导


def test_tutorial_steps_and_session_flag(mech_env) -> None:
    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "r9_test", "seed": 10}).json()["id"]
    tutorial = client.get(f"/api/sessions/{session_id}/view").json()["tutorial"]
    assert tutorial["enabled"] is True and tutorial["total"] == 3
    assert [row["title"] for row in tutorial["steps"]] == ["看国势", "批答奏疏"] and tutorial["steps"][0]["target"] == "desk.resources"
    off = client.put(f"/api/sessions/{session_id}/flags", json={"tutorial": False}).json()
    assert off["session_flags"] == {"tutorial": False} and off["tutorial"]["steps"] == []
    assert client.get(f"/api/sessions/{session_id}/view").json()["tutorial"]["enabled"] is False
    client.put(f"/api/sessions/{session_id}/flags", json={"tutorial": True})
    client.post(f"/api/sessions/{session_id}/turn")
    assert [row["title"] for row in client.get(f"/api/sessions/{session_id}/view").json()["tutorial"]["steps"]] == ["下诏"]


# ---------------------------------------------------------------- MCP


async def _call(client: Client, name: str, **arguments) -> tuple[bool, str]:
    result = await client.call_tool(name, arguments)
    return bool(result.is_error), "\n".join(block.text for block in result.content if getattr(block, "text", None))


def test_mcp_round9_tools(mech_env) -> None:
    async def run() -> None:
        async with Client(build_server(GameService(), mode="client")) as client:
            error, text = await _call(client, "new_game", scenario="r9_test", seed=11)
            session_id = re.search(r"session_id=([0-9a-f]{32})", text).group(1)
            assert "朝会日程（本季）：召对 0/2、廷议 0/1" in text and "## 政令卡" in text
            error, cards = await _call(client, "policy_cards", session_id=session_id)
            assert not error and "[card_march] 出征（军务；可用）" in cards and "`eunuch` 高内侍" in cards
            error, sent = await _call(client, "audience", session_id=session_id, character="westlord", question="边情如何？")
            assert not error and "使者已发，回奏于下季送达" in sent and "# 人设卡" not in sent
            error, refused = await _call(client, "court_debate", session_id=session_id, topic="边镇增兵议", participants=["westlord"])
            assert error and "NOT_PRESENT" in refused
            error, debate = await _call(
                client, "court_debate", session_id=session_id, topic="边镇增兵议", participants=["chancellor"], written=["westlord"]
            )
            assert not error and "## 书面议状" in debate and "西帅（西陲）" in debate
            error, draft = await _call(
                client,
                "draft_edict",
                session_id=session_id,
                cards=[{"card_id": "card_hold", "params": {"army": "guard"}}],
                ops=[{"op": "appoint", "character": "westlord", "office": "censor"}],
            )
            assert not error and "(card) 禁军 改取坚守之势" in draft
            assert "延期原因（驿程）：驿程遥远，令达于下季" in draft
            edict_id = re.search(r"edict_id=(edict-[0-9a-f]+)", draft).group(1)
            error, _ = await _call(client, "confirm_edict", session_id=session_id, edict_id=edict_id)
            assert not error
            error, report = await _call(client, "end_turn", session_id=session_id)
            assert not error and "## 书面议状送达" in report
            error, brief = await _call(client, "briefing", session_id=session_id)
            assert "## 回奏" in brief and "### 西帅的回奏" in brief and "人设卡：西帅" in brief and "所问「边情如何？」" in brief
            assert "## 书面议状" in brief

    asyncio.run(run())


def test_public_view_hides_internal_courier_records(mech_env) -> None:
    service, session_id = new_game(12)
    session = service.session(session_id)
    session.append_op({"op": "set_flag", "key": "force_west", "value": True})
    session.append_op({"op": "set_flag", "key": "west_alarm", "value": True})
    end_turn(service, session_id)
    state = session.head_state()
    assert "_internal:courier_reports" in state.flags and "_internal:courier:memorial:m_west_alarm" in state.flags
    public = public_view(state, session.seed)
    assert not any(str(key).startswith("_internal:") for key in public["flags"])
    assert state_hash(state)  # 内部记录随状态进入哈希
    assert [row["id"] for row in visible_memorials(session.scenario, state)].count("m_west") == 1
