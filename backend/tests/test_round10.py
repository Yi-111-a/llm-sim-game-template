"""第 10 轮（GPT gpt-10 报告）引擎修复：谏言所主之案与赞否一致、旧衔不重复列现职、事件中启程者的预计抵达回合、
延期任命的「令达后即日视事」。"""

import asyncio
import json
import random

from conftest import FIXTURE_SCENARIOS
from test_round6 import _review_memorial, _scenario, _service_with
from test_round9 import end_turn, new_game

from simcore.domain.apply import apply
from simcore.domain.memorials import summarize_ops
from simcore.domain.rules import current_title
from simcore.domain.turn import resolve_turn
from simcore.scenario.loader import build_initial_state, load_scenario


def test_advisor_backing_another_option_is_counted_as_opposing() -> None:
    # gpt-10 T0：杨国忠谏言主「留京宿卫」，所议之案为「郭子仪接掌范阳」，却被标为「赞成」
    scenario = _scenario()
    memorial = _review_memorial()
    memorial["advisors"][0]["option"] = "lenient"  # 赞成者（郭将军）的谏言明主「依例考课」
    memorial["advisors"][1]["option"] = "strict"  # 反对者（李相）的谏言却主所议之案
    scenario["memorials"].append(memorial)
    service = _service_with(scenario)
    session_id, _ = service.create_session("mech_test", 7)
    brief = service.debate_brief(session_id, "", ["general", "chancellor"], "m_review", option_id="strict")
    rows = {row["character"]: row for row in brief["stances"]}
    assert rows["general"]["target_option_id"] == "lenient"
    assert rows["general"]["stance"] == "oppose" and rows["general"]["stance_label"] == "反对"
    assert rows["general"]["against_option_id"] == "strict"
    assert rows["chancellor"]["target_option_id"] == "strict" and rows["chancellor"]["stance"] == "support"


def test_former_title_lists_only_lapsed_offices() -> None:
    # gpt-10 T8：陈希烈「左相、判度支（旧衔：左相，已非现职）」
    scenario = load_scenario(FIXTURE_SCENARIOS / "mech_test")
    state = build_initial_state(scenario)
    state.characters["chancellor"].title = "宰相"
    state, _ = apply({"op": "appoint", "character": "chancellor", "office": "censor"}, state)
    assert current_title(state, "chancellor", scenario) == ("宰相、御史", "")
    state, _ = apply({"op": "dismiss", "character": "chancellor", "office": "chancellor"}, state)
    assert current_title(state, "chancellor", scenario) == ("御史", "宰相")


def test_traveller_started_by_an_event_is_due_after_the_next_movement() -> None:
    # gpt-10 T0→T1：史思明于结算中的事件启程「约一季」，T1 简报却写「预计第1回合抵达」，T2 才到
    scenario = load_scenario(FIXTURE_SCENARIOS / "mech_test")
    scenario["events"].append(
        {"id": "summon", "title": "召还", "when": {"turn": 1}, "gravity": 1, "effects": [{"op": "move_character", "character": "general", "to_region": "pass"}]}
    )
    state = build_initial_state(scenario)
    state, _ = resolve_turn(state, random.Random(3), scenario)
    assert state.turn == 1 and state.characters["general"].in_transit["eta_turn"] == 2
    state, report = resolve_turn(state, random.Random(4), scenario)
    assert state.characters["general"].region == "pass" and any(fact.kind == "arrival" for fact in report.facts)


def test_delayed_posting_takes_effect_when_the_order_arrives(mech_env) -> None:
    # gpt-10 #9：封常清任安西被驿程延期（约 54 日），草案与确认回执却写「人在军中，即日视事」
    service, session_id = new_game(5)
    session = service.session(session_id)
    session.append_op({"op": "set_army_commander", "army": "west_army", "character": None})
    # 远地任帅的奏疏选项预览：整组照驿程延期，也写「令达后」
    order = {"op": "set_army_commander", "army": "west_army", "character": "westlord"}
    assert summarize_ops([order], session.head_state(), pending=True)[0].endswith("人在军中，令达后即日视事")
    receipt = asyncio.run(service.draft_edict(
        session_id, ops=[order, {"op": "set_army_commander", "army": "guard", "character": "general"}]
    ))
    far, near = receipt["clauses"]
    assert far["status"] == "delayed" and far["delay_kind"] == "courier"
    assert far["summary"].endswith("人在军中，令达后即日视事")
    assert near["status"] == "applied" and near["summary"].endswith("人在军中，即日视事") and "令达后" not in near["summary"]
    confirmed = service.confirm_edict(session_id, receipt["id"])
    assert "令达后即日视事" in json.dumps(confirmed["clauses"], ensure_ascii=False)
    assert any("令达后即日视事" in line for line in service._pending_waits(service.session(session_id).head_state()))
    facts = [fact["text"] for fact in end_turn(service, session_id)["report"]["facts"]]
    # 令到之季执行：事实写「即日视事」，不再说「令达后」
    applied = next(text for text in facts if "西帅" in text and "视事" in text)
    assert applied.endswith("人在军中，即日视事")


def test_offline_speaker_does_not_name_himself_as_the_absent_commander() -> None:
    # 召对安禄山（人在京师）：从前回「今范阳之兵九万余，主帅禄山未至，副将暂摄」
    from simcore.agent.offline import _army_phrase

    row = "范阳军 91000，驻范阳，属唐廷，统帅安禄山不在军中，副将暂摄"
    assert "主帅禄山未至" in _army_phrase(row)
    own = _army_phrase(row, speaker=("安禄山", "臣胡人"))
    assert "臣胡人不在军中，副将暂摄" in own and "禄山未至" not in own
    assert "皆在臣麾下" in _army_phrase("范阳军 91000，驻范阳，属唐廷，统帅安禄山", speaker=("安禄山", "臣"))
