"""§1 引擎扩展：新 op、兼职、条件 DSL、事件抉择、回合顺序、战斗、AI 行军、计数器、结局、历法。"""

import random
from typing import Any

import pytest

from simcore.domain.apply import OpRejected, apply
from simcore.domain.events import condition_errors, matches
from simcore.domain.invariants import check_invariants
from simcore.domain.models import (
    Army,
    Calendar,
    Character,
    ChoiceOption,
    Faction,
    Office,
    Outcome,
    PendingChoice,
    Region,
    Resource,
    WorldState,
)
from simcore.domain.rules import Rejected, validate
from simcore.domain.turn import GameOver, judge_outcome, resolve_turn
from simcore.scenario.loader import build_initial_state, validate_scenario


def make_world() -> WorldState:
    return WorldState(
        scenario_id="mech",
        turn=0,
        calendar=Calendar(year=751, month=1, season="春"),
        resources={
            "treasury": Resource(id="treasury", name="国库", amount=100, minimum=0),
            "authority": Resource(id="authority", name="君威", amount=50, minimum=0, maximum=100),
        },
        regions={
            "capital": Region(id="capital", name="京畿", adjacency={"pass": 1}, stats={"defense": 50, "tax": 10}, controller="court"),
            "pass": Region(id="pass", name="雄关", adjacency={"capital": 1, "plain": 1}, stats={"defense": 85, "tax": 2}, controller="court"),
            "plain": Region(id="plain", name="平原", adjacency={"pass": 1}, stats={"defense": 10, "tax": 5}, controller="court"),
        },
        characters={
            "a": Character(id="a", name="甲", region="capital", traits={"ability": 8, "loyalty": 6}, bloc="xiang"),
            "b": Character(id="b", name="乙", region="capital", traits={"ability": 8, "loyalty": 9}),
        },
        offices={
            "o1": Office(id="o1", name="右相"),
            "o2": Office(id="o2", name="河东节度"),
            "o3": Office(id="o3", name="范阳节度"),
        },
        factions={
            "court": Faction(id="court", name="朝廷", relations={"rebels": -50}),
            "rebels": Faction(id="rebels", name="叛军", relations={"court": -50}),
        },
        armies={
            "guard": Army(id="guard", name="禁军", faction="court", region="capital", strength=1000, commander="b"),
        },
        rules={"max_concurrent_offices": 2},
    )


def resolve(state: WorldState, scenario: dict[str, Any] | None = None, seed: int = 1) -> tuple[WorldState, Any]:
    updated, report = resolve_turn(state, random.Random(seed), scenario)
    assert check_invariants(updated, previous=state) == []
    return updated, report


def rejected_code(op: dict[str, Any], state: WorldState) -> str:
    result = validate(op, state)
    assert isinstance(result, Rejected), result
    return result.code


# ---- 新 op ------------------------------------------------------------------------


def test_adjust_army_floors_at_zero_and_reports_fact() -> None:
    state, facts = apply({"op": "adjust_army", "army": "guard", "delta": -5000, "reason": "溃散"}, make_world())
    assert state.armies["guard"].strength == 0
    assert facts[0].before == 1000 and facts[0].after == 0
    assert rejected_code({"op": "adjust_army", "army": "nope", "delta": 1}, state) == "UNKNOWN_ENTITY"


def test_set_army_faction_requires_existing_faction() -> None:
    state, _ = apply({"op": "set_army_faction", "army": "guard", "faction": "rebels"}, make_world())
    assert state.armies["guard"].faction == "rebels"
    assert rejected_code({"op": "set_army_faction", "army": "guard", "faction": "ghost"}, state) == "UNKNOWN_ENTITY"


def test_set_army_commander_requires_living_character_and_allows_null() -> None:
    state = make_world()
    state, _ = apply({"op": "set_army_commander", "army": "guard", "character": "a"}, state)
    assert state.armies["guard"].commander == "a"
    state, _ = apply({"op": "set_army_commander", "army": "guard", "character": None}, state)
    assert state.armies["guard"].commander is None
    state, _ = apply({"op": "set_character_status", "character": "b", "status": "dead"}, state)
    assert rejected_code({"op": "set_army_commander", "army": "guard", "character": "b"}, state) == "CHARACTER_UNAVAILABLE"
    assert rejected_code({"op": "set_army_commander", "army": "guard", "character": "ghost"}, state) == "UNKNOWN_ENTITY"


def test_adjust_trait_counter_and_region_controller_ops() -> None:
    state = make_world()
    state, _ = apply({"op": "adjust_trait", "character": "a", "trait": "fear", "delta": 3}, state)
    assert state.characters["a"].traits["fear"] == 3
    state, _ = apply({"op": "adjust_counter", "key": "streak", "delta": 2}, state)
    state, _ = apply({"op": "adjust_counter", "key": "streak", "delta": 1}, state)
    assert state.counters["streak"] == 3
    state, _ = apply({"op": "set_counter", "key": "streak", "value": 0}, state)
    assert state.counters["streak"] == 0
    state, _ = apply({"op": "set_region_controller", "region": "plain", "faction": "rebels"}, state)
    assert state.regions["plain"].controller == "rebels"
    assert rejected_code({"op": "adjust_trait", "character": "ghost", "trait": "fear", "delta": 1}, state) == "UNKNOWN_ENTITY"
    assert rejected_code({"op": "set_region_controller", "region": "ghost", "faction": "court"}, state) == "UNKNOWN_ENTITY"


def make_choice(state: WorldState) -> WorldState:
    state.pending_choices.append(
        PendingChoice(
            id="choice-x-1",
            event_id="x",
            title="请饷",
            options=[
                ChoiceOption(id="grant", text="准", effects=[{"op": "adjust_resource", "resource": "treasury", "delta": -30}]),
                ChoiceOption(id="vip", text="特赏", conditions={"kind": "resource", "id": "authority", "operator": "gte", "value": 90}),
                ChoiceOption(id="broken", text="坏效果", effects=[{"op": "adjust_resource", "resource": "ghost", "delta": 1}]),
            ],
            default_option="grant",
            created_turn=0,
            deadline_turn=1,
        )
    )
    return state


def test_choose_event_option_validates_and_applies_effects() -> None:
    state = make_choice(make_world())
    assert rejected_code({"op": "choose_event_option", "choice_id": "nope", "option_id": "grant"}, state) == "UNKNOWN_ENTITY"
    assert rejected_code({"op": "choose_event_option", "choice_id": "choice-x-1", "option_id": "nope"}, state) == "UNKNOWN_ENTITY"
    assert rejected_code({"op": "choose_event_option", "choice_id": "choice-x-1", "option_id": "vip"}, state) == "CONDITION_FAILED"
    updated, facts = apply({"op": "choose_event_option", "choice_id": "choice-x-1", "option_id": "grant"}, state)
    assert updated.pending_choices == []
    assert updated.resources["treasury"].amount == 70
    assert updated.flags["event:x:choice"] == "grant"
    assert updated.flags["choice:choice-x-1"] == "grant"
    assert facts[0].kind == "choice"
    broken, facts = apply({"op": "choose_event_option", "choice_id": "choice-x-1", "option_id": "broken"}, state)
    assert any(fact.kind == "effect_skipped" for fact in facts)
    assert broken.pending_choices == []


def test_multi_office_appoint_dismiss_and_limit() -> None:
    state = make_world()
    state, _ = apply({"op": "appoint", "character": "a", "office": "o1"}, state)
    state, _ = apply({"op": "appoint", "character": "a", "office": "o2"}, state)
    assert state.characters["a"].offices == ["o1", "o2"]
    assert state.characters["a"].office == "o1"
    assert rejected_code({"op": "appoint", "character": "a", "office": "o3"}, state) == "OFFICE_HELD"
    state, _ = apply({"op": "dismiss", "character": "a", "office": "o1"}, state)
    assert state.characters["a"].office == "o2"
    assert state.characters["a"].offices == ["o2"]
    assert state.offices["o1"].holder is None
    assert check_invariants(state) == []
    state.rules["max_concurrent_offices"] = 1
    assert rejected_code({"op": "appoint", "character": "a", "office": "o3"}, state) == "OFFICE_HELD"


def test_non_alive_status_clears_offices_command_and_transit() -> None:
    state = make_world()
    state, _ = apply({"op": "appoint", "character": "b", "office": "o1"}, state)
    state, _ = apply({"op": "appoint", "character": "b", "office": "o2"}, state)
    state, _ = apply({"op": "move_character", "character": "b", "to_region": "pass"}, state)
    state, _ = apply({"op": "set_character_status", "character": "b", "status": "imprisoned"}, state)
    person = state.characters["b"]
    assert person.offices == [] and person.office is None and person.in_transit is None
    assert state.offices["o1"].holder is None and state.offices["o2"].holder is None
    assert state.armies["guard"].commander is None
    assert check_invariants(state) == []


def test_set_flag_records_turn_and_game_over_blocks_writes() -> None:
    state = make_world()
    state, _ = apply({"op": "set_flag", "key": "k", "value": True}, state, turn=3)
    assert state.flag_turns["k"] == 3
    state.outcome = Outcome(kind="fail", id="x", title="亡", turn=0)
    with pytest.raises(OpRejected) as caught:
        apply({"op": "adjust_resource", "resource": "treasury", "delta": 1}, state)
    assert caught.value.code == "GAME_OVER"
    with pytest.raises(GameOver):
        resolve_turn(state, random.Random(1))


# ---- 条件 DSL -----------------------------------------------------------------------


def test_condition_kinds_and_operators() -> None:
    state = make_world()
    state, _ = apply({"op": "set_flag", "key": "seen", "value": True}, state, turn=1)
    state, _ = apply({"op": "set_flag", "key": "level", "value": 3}, state)
    state.counters["broke"] = 2
    state.characters["a"].traits["relation:b"] = -30
    state.offices["o1"].holder = "a"
    state.characters["a"].offices = ["o1"]
    state.characters["a"].office = "o1"
    cases = [
        ({"kind": "turn", "value": 3}, True),
        ({"kind": "turn", "operator": "gte", "value": 4}, False),
        ({"kind": "resource", "id": "treasury", "value": 100}, True),  # 兼容：resource 缺省 gte
        ({"kind": "resource", "id": "treasury", "operator": "lt", "value": 100}, False),
        ({"kind": "flag", "key": "seen"}, True),
        ({"kind": "flag", "key": "seen", "operator": "exists"}, True),
        ({"kind": "flag", "key": "missing", "operator": "absent"}, True),
        ({"kind": "flag", "key": "level", "operator": "gt", "value": 2}, True),
        ({"kind": "flag_age", "key": "seen", "operator": "gte", "value": 2}, True),  # 在第 3 回合判定时
        ({"kind": "flag_age", "key": "missing", "operator": "gte", "value": 0}, False),
        ({"kind": "counter", "key": "broke", "operator": "gte", "value": 2}, True),
        ({"kind": "counter", "key": "absent", "value": 0}, True),
        ({"kind": "region_stat", "region": "pass", "stat": "defense", "operator": "gte", "value": 80}, True),
        ({"kind": "region_controller", "region": "plain", "faction": "court"}, True),
        ({"kind": "character_status", "character": "a", "value": "alive"}, True),
        ({"kind": "character_trait", "character": "a", "trait": "ability", "operator": "gt", "value": 7}, True),
        ({"kind": "character_region", "character": "a", "region": "capital"}, True),
        ({"kind": "office_holder", "office": "o1", "character": "a"}, True),
        ({"kind": "office_holder", "office": "o2", "character": None}, True),
        ({"kind": "army_strength", "army": "guard", "operator": "gte", "value": 1000}, True),
        ({"kind": "army_strength", "faction": "rebels", "operator": "eq", "value": 0}, True),
        ({"kind": "relation", "a": "a", "b": "b", "operator": "lt", "value": 0}, True),
        ({"kind": "relation", "a": "court", "b": "rebels", "operator": "lte", "value": -50}, True),
        ({"not": {"kind": "flag", "key": "seen"}}, False),
        ({"any": [{"kind": "flag", "key": "missing"}, {"kind": "flag", "key": "seen"}]}, True),
        ({"kind": "unknown_kind"}, False),
    ]
    for condition, expected in cases:
        assert matches(condition, state, 3) is expected, condition


def test_condition_validator_reports_unknown_kinds_and_operators() -> None:
    assert condition_errors({"kind": "flag", "key": "x", "operator": "absent"}, "c") == []
    assert condition_errors({"kind": "nope"}, "c")
    assert condition_errors({"kind": "counter", "key": "x", "operator": "between", "value": 1}, "c")
    assert condition_errors({"all": [{"kind": "turn", "value": 1}, {"kind": "bad"}]}, "c")


# ---- 事件 -------------------------------------------------------------------------


CHOICE_EVENT = {
    "id": "petition",
    "title": "请饷",
    "text": "边将请饷",
    "when": {"turn": 1},
    "gravity": 1,
    "effects": [{"op": "set_flag", "key": "petition_seen", "value": True}],
    "choices": [
        {"id": "grant", "text": "准", "effects": [{"op": "adjust_resource", "resource": "treasury", "delta": -30}]},
        {"id": "deny", "text": "驳", "effects": [{"op": "adjust_resource", "resource": "authority", "delta": 2}]},
    ],
    "default_choice": "deny",
}


def test_event_choice_opens_pending_choice_and_player_can_choose() -> None:
    state, report = resolve(make_world(), {"events": [CHOICE_EVENT]})
    assert report.events_fired == ["petition"]
    assert [row["id"] for row in report.choices_opened] == ["choice-petition-1"]
    choice = state.pending_choices[0]
    assert (choice.created_turn, choice.deadline_turn, choice.default_option) == (1, 2, "deny")
    state, _ = apply({"op": "choose_event_option", "choice_id": choice.id, "option_id": "grant"}, state)
    state, report = resolve(state, {"events": [CHOICE_EVENT]})
    assert state.resources["treasury"].amount == 70
    assert state.pending_choices == []
    assert not any(fact.kind == "choice_default" for fact in report.facts)


def test_unanswered_choice_runs_default_at_start_of_next_resolution() -> None:
    state, _ = resolve(make_world(), {"events": [CHOICE_EVENT]})
    state.ledger = []
    state, report = resolve(state, {"events": [CHOICE_EVENT]})
    assert report.facts[0].kind == "choice_default"
    assert "驳" in report.facts[0].text
    assert state.resources["authority"].amount == 52
    assert state.flags["event:petition:choice"] == "deny"
    assert state.pending_choices == []


def test_choice_without_ids_gets_sequential_ids() -> None:
    event = {"id": "legacy", "title": "旧式", "gravity": 1, "choices": [{"text": "甲"}, {"text": "乙"}]}
    state, _ = resolve(make_world(), {"events": [event]})
    assert [option.id for option in state.pending_choices[0].options] == ["option_1", "option_2"]
    assert state.pending_choices[0].default_option == "option_1"


def test_repeatable_event_respects_cooldown_and_once_only_default() -> None:
    once = {"id": "once", "title": "一次", "gravity": 1, "effects": [{"op": "adjust_counter", "key": "once", "delta": 1}]}
    repeat = {"id": "rep", "title": "复", "gravity": 1, "repeatable": True, "cooldown": 2, "effects": [{"op": "adjust_counter", "key": "rep", "delta": 1}]}
    state = make_world()
    fired = []
    for _ in range(6):
        state, report = resolve(state, {"events": [once, repeat]})
        fired.append(report.events_fired)
    assert state.counters["once"] == 1
    assert state.counters["rep"] == 3
    assert fired[0] == ["once", "rep"] and fired[1] == [] and fired[2] == ["rep"]


def test_repeatable_choice_event_does_not_stack_pending_choices() -> None:
    event = {**CHOICE_EVENT, "when": {}, "repeatable": True, "cooldown": 1, "choice_deadline": 3}
    state, _ = resolve(make_world(), {"events": [event]})
    state, report = resolve(state, {"events": [event]})
    assert report.events_fired == []
    assert len(state.pending_choices) == 1


# ---- 回合顺序、收入、计数器、结局 --------------------------------------------------------------


def test_turn_report_order_and_multiple_incomes() -> None:
    state = make_choice(make_world())
    state.ledger = []
    from simcore.domain.apply import queue_delayed

    state = queue_delayed(state, [{"op": "adjust_resource", "resource": "authority", "delta": 1}], 1)
    scenario = {
        "formulas": {
            "income": {
                "tax": {"resource": "treasury", "formula": "tax"},
                "canal": {"resource": "treasury", "formula": "5", "per_region": False, "label": "漕运"},
                "border": {"resource": "treasury", "formula": "controlled * 100", "controlled_only": True},
            }
        }
    }
    state.rules["player_faction"] = "court"
    state.regions["plain"].controller = "rebels"
    updated, report = resolve(state, scenario)
    kinds = [fact.kind for fact in report.facts]
    assert kinds.index("choice_default") < kinds.index("resource")
    names = [line.name for line in report.ledger_lines if line.kind == "income"]
    assert "漕运" in names and "京畿税入" in names and "平原border" not in names
    assert updated.resources["treasury"].amount == pytest.approx(100 - 30 + 17 + 5 + 200)


def test_streak_and_count_counters() -> None:
    scenario = {
        "counters": [
            {"id": "broke", "mode": "streak", "conditions": {"kind": "resource", "id": "treasury", "operator": "lte", "value": 0}},
            {"id": "seen", "mode": "count", "conditions": {"kind": "flag", "key": "mark"}},
        ]
    }
    state = make_world()
    state.resources["treasury"].amount = 0
    state.flags["mark"] = True
    state, _ = resolve(state, scenario)
    state, _ = resolve(state, scenario)
    assert state.counters == {"broke": 2, "seen": 2}
    state.resources["treasury"].amount = 10
    state.flags["mark"] = False
    state, _ = resolve(state, scenario)
    assert state.counters == {"broke": 0, "seen": 2}


def test_endings_priority_fallback_and_overtime() -> None:
    endings = [
        {"id": "late", "kind": "ending", "title": "后", "priority": 5, "conditions": {"kind": "flag", "key": "both"}},
        {"id": "first", "kind": "fail", "title": "先", "priority": 1, "conditions": {"kind": "flag", "key": "both"}},
        {"id": "storm", "kind": "ending", "title": "山雨欲来"},
    ]
    definition = {"endings": endings, "game_length": {"last_turn": 3, "overtime_flag": "war", "overtime_last_turn": 5, "fallback_ending": "storm"}}
    state = make_world()
    assert judge_outcome(state, definition, 2) is None
    assert judge_outcome(state, definition, 3).id == "storm"
    state.flags["war"] = True
    assert judge_outcome(state, definition, 3) is None
    assert judge_outcome(state, definition, 5).id == "storm"
    state.flags["both"] = True
    outcome = judge_outcome(state, definition, 1)
    assert (outcome.id, outcome.kind) == ("first", "fail")
    finished, report = resolve(state, {"scenario": definition})
    assert finished.outcome is not None and report.outcome == finished.outcome
    with pytest.raises(GameOver):
        resolve_turn(finished, random.Random(1), {"scenario": definition})


def test_mech_fixture_calendar_labels_and_era_switch(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    assert state.calendar.label == "天宝十载春"
    labels = []
    for _ in range(7):
        state, _ = resolve(state, mech_scenario)
        labels.append(state.calendar.label)
    assert labels[:4] == ["天宝十载夏", "天宝十载秋", "天宝十载冬", "天宝十一载春"]
    assert labels[5] == "至德元年秋"  # 年号年中改元，改用该年号自己的 turn_format
    assert labels[6] == "至德元年冬"


def test_chongzhen_t0_label_falls_back_to_year() -> None:
    from simcore.scenario.loader import load_scenario

    scenario = load_scenario("../scenarios/chongzhen_1627")
    state = build_initial_state(scenario)
    assert state.calendar.label == "1627年冬"


# ---- 战斗与 AI 行军 ---------------------------------------------------------------------


def battle_world() -> WorldState:
    state = make_world()
    state.armies = {
        "def": Army(id="def", name="守军", faction="court", region="plain", strength=1000),
        "att": Army(id="att", name="叛军", faction="rebels", region="plain", strength=5000),
    }
    return state


def test_battle_loser_retreats_and_attacker_captures_region() -> None:
    state, report = resolve(battle_world())
    battle = report.battles[0]
    assert battle["result"] == "attacker_won" and battle["captured"] is True
    assert state.armies["def"].strength == pytest.approx(700)
    assert state.armies["def"].region == "pass"
    assert state.armies["att"].strength == pytest.approx(4500)
    assert state.regions["plain"].controller == "rebels"


def test_battle_without_retreat_routes_and_is_seed_deterministic() -> None:
    state = battle_world()
    state.regions["pass"].controller = "rebels"
    first, report_a = resolve(state, seed=4)
    second, report_b = resolve(state, seed=4)
    assert report_a.battles == report_b.battles
    assert first.armies["def"].strength == 0
    assert report_a.battles[0]["retreats"]["def"] is None


def test_fortress_requires_double_power_else_stalemate() -> None:
    state = make_world()
    state.armies = {
        "def": Army(id="def", name="关军", faction="court", region="pass", strength=1000),
        "att": Army(id="att", name="叛军", faction="rebels", region="pass", strength=2500),
    }
    updated, report = resolve(state)
    assert report.battles[0]["result"] == "stalemate"
    assert updated.armies["def"].strength == 1000 and updated.armies["att"].strength == 2500
    state.armies["att"].strength = 10000
    updated, report = resolve(state)
    assert report.battles[0]["result"] == "attacker_won"


def test_wars_list_makes_neutral_factions_fight_and_defender_bonus_only_for_controller() -> None:
    state = battle_world()
    state.factions["court"].relations = {}
    state.factions["rebels"].relations = {}
    _, report = resolve(state)
    assert report.battles == []
    _, report = resolve(state, {"scenario": {"wars": [["court", "rebels"]]}})
    assert report.battles[0]["defender"] == "court"
    assert report.battles[0]["defense_bonus"] == 10


def test_ai_route_advances_holds_when_outnumbered_and_occupies_on_arrival() -> None:
    state = make_world()
    state.armies = {"att": Army(id="att", name="叛军", faction="rebels", region="plain", strength=5000)}
    state.regions["plain"].controller = "rebels"
    routes = {"scenario": {"ai_routes": [{"faction": "rebels", "route": ["plain", "pass", "capital"], "hold_if_outnumbered": True}]}}
    state, report = resolve(state, routes)
    assert state.armies["att"].in_transit == {"to_region": "pass", "eta_turn": 2}
    state, report = resolve(state, routes)
    assert state.regions["pass"].controller == "rebels"  # 新抵达且无守军即占领
    assert state.armies["att"].in_transit == {"to_region": "capital", "eta_turn": 3}
    blocked = make_world()
    blocked.armies = {
        "att": Army(id="att", name="叛军", faction="rebels", region="plain", strength=500),
        "def": Army(id="def", name="关军", faction="court", region="pass", strength=5000),
    }
    blocked, report = resolve(blocked, routes)
    assert blocked.armies["att"].in_transit is None
    assert any(fact.kind == "ai_hold" for fact in report.facts)


def test_ai_route_conditions_gate_movement() -> None:
    state = make_world()
    state.armies = {"att": Army(id="att", name="叛军", faction="rebels", region="plain", strength=5000)}
    routes = {"scenario": {"ai_routes": [{"faction": "rebels", "route": ["plain", "pass"], "conditions": {"kind": "flag", "key": "go"}}]}}
    state, _ = resolve(state, routes)
    assert state.armies["att"].in_transit is None


def test_stationary_hostile_army_does_not_capture_without_arriving() -> None:
    state = make_world()
    state.armies = {"att": Army(id="att", name="叛军", faction="rebels", region="plain", strength=5000)}
    state, _ = resolve(state)
    assert state.regions["plain"].controller == "court"


# ---- 危机计与剧本校验 ----------------------------------------------------------------------


def test_crisis_terms_are_reported_each_turn(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.resources["corruption"].amount = 60
    state, report = resolve(state, mech_scenario)
    terms = {row["id"]: row["value"] for row in report.crisis["terms"]}
    assert terms == {"amb": 0.05, "cor": 1.0}  # 结算全部完成后（异志漂移 +0.5 之后）计算一次
    assert report.crisis["band"] == "承平"
    assert report.crisis["value"] == pytest.approx(31.0)


def test_validator_reports_mechanics_errors(mech_scenario) -> None:
    assert validate_scenario(mech_scenario) == []
    broken = {**mech_scenario}
    broken["events"] = [*mech_scenario["events"], {"id": "bad", "title": "坏", "choices": [{"id": "x"}, {"id": "x"}], "default_choice": "y"}]
    broken["memorials"] = [{"id": "m", "title": "无选项", "advisors": [{"character": "ghost"}], "options": []}]
    broken["scenario"] = {**mech_scenario["scenario"], "crisis": {"resource": "ghost"}, "ai_routes": [{"faction": "rebels", "route": ["atlantis"]}]}
    errors = validate_scenario(broken)
    for needle in ("选项 id 重复", "default_choice", "谏言人不存在", "至少需要一个选项", "crisis.resource", "ai_routes 地区不存在"):
        assert any(needle in error for error in errors), needle


def test_invariant_checker_detects_inconsistencies() -> None:
    state = make_world()
    state.offices["o1"].holder = "a"
    problems = check_invariants(state)
    assert any("未记此职" in problem for problem in problems)
    state = make_world()
    state.characters["b"].status = "dead"
    state.resources["treasury"].amount = -1
    state.armies["guard"].strength = -5
    problems = check_invariants(state)
    assert any("统兵" in problem for problem in problems)
    assert any("下限" in problem for problem in problems)
    assert any("兵力为负" in problem for problem in problems)
    state = make_choice(make_world())
    state.flags["choice:choice-x-1"] = "grant"
    assert any("已处置" in problem for problem in check_invariants(state))
    before = make_world()
    before.outcome = Outcome(kind="ending", id="e", title="终", turn=0)
    assert any("终局" in problem for problem in check_invariants(make_world(), previous=before))
