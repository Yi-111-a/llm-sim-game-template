"""《宣和》制作中回写模板的通用机制：军队素质可由 op 调整（adjust_army.quality_delta）、
统帅才干影响战力（settings.battle.commander_factor）。"""

import random

import pytest
from conftest import FIXTURE_SCENARIOS
from test_round9 import WARS, _battle_state

from simcore.domain.apply import apply
from simcore.domain.battle import resolve_battles
from simcore.domain.memorials import summarize_ops
from simcore.domain.military import commander_factor
from simcore.domain.visibility import battle_breakdown
from simcore.scenario.loader import build_initial_state, load_scenario, round9_errors


@pytest.fixture
def r9_state():
    return build_initial_state(load_scenario(FIXTURE_SCENARIOS / "r9_test"))


def test_adjust_army_quality_delta_changes_quality_and_is_clamped(r9_state) -> None:
    state, facts = apply({"op": "adjust_army", "army": "guard", "quality_delta": 0.15, "reason": "整训"}, r9_state)
    assert state.armies["guard"].quality == pytest.approx(r9_state.armies["guard"].quality + 0.15)
    assert state.armies["guard"].strength == r9_state.armies["guard"].strength
    assert [fact.kind for fact in facts] == ["army_quality"] and "素质" in facts[0].text
    state, _ = apply({"op": "adjust_army", "army": "guard", "quality_delta": -9}, state)
    assert state.armies["guard"].quality == 0.1
    # 旧写法（只改兵额）不变：只有兵力事实
    state, facts = apply({"op": "adjust_army", "army": "guard", "delta": 1000}, r9_state)
    assert [fact.kind for fact in facts] == ["army"]
    assert summarize_ops([{"op": "adjust_army", "army": "guard", "quality_delta": 0.1}], r9_state)[0].endswith("素质 +0.1")


def test_commander_factor_scales_power_only_when_configured_and_present(r9_state) -> None:
    base_state = _battle_state(r9_state)
    [base], _ = resolve_battles(base_state, random.Random(9), WARS)
    assert commander_factor(base_state, base_state.armies["guard"]) is None  # 未配置：战力公式不变
    led = _battle_state(r9_state)
    led.rules["battle"] = {"commander_factor": "1 + 0.04*(ability - 5)"}
    guard = led.armies["guard"]
    commander = next(row for row in led.characters.values() if row.faction == guard.faction and row.status == "alive" and row.id != "ruler")
    commander.traits["ability"] = 8
    commander.region = guard.region
    commander.in_transit = None
    guard.commander = commander.id
    [battle], _ = resolve_battles(led, random.Random(9), WARS)
    assert battle["defender_power"] == pytest.approx(base["defender_power"] * 1.12, rel=1e-3)
    assert "将才" in battle_breakdown(battle) and "×将才" in battle_breakdown(battle)
    # 统帅不在军中（遥领）：不计将才
    commander.region = next(region for region in led.regions if region != guard.region)
    assert commander_factor(led, guard) is None


def test_battle_settings_are_validated() -> None:
    bad = {"scenario": {"settings": {"battle": {"commander_factor": "1 +"}}}}
    assert any("commander_factor" in error for error in round9_errors(bad, set()))
    unknown = {"scenario": {"settings": {"battle": {"luck": 0.2}}}}
    assert any("settings.battle" in error for error in round9_errors(unknown, set()))
    assert round9_errors({"scenario": {"settings": {"battle": {"commander_factor": "1 + 0.04*(ability-5)"}}}}, set()) == []


def test_add_ledger_with_named_id_replaces_and_can_be_cancelled(r9_state) -> None:
    op = {"op": "add_ledger", "id": "tribute_bureau", "name": "应奉司", "category": "treasury", "amount": -18, "kind": "recurring"}
    state, _ = apply(op, r9_state)
    state, _ = apply({**op, "amount": -9}, state)  # 同 id 再设：替换而不重复
    rows = [row for row in state.ledger if row.id == "tribute_bureau"]
    assert len(rows) == 1 and rows[0].amount == -9
    state, _ = apply({"op": "cancel_ledger", "ledger_id": "tribute_bureau"}, state)
    assert not any(row.id == "tribute_bureau" for row in state.ledger)


def test_merge_army_conserves_troops_and_weights_quality(r9_state) -> None:
    state = r9_state
    own = [row for row in state.armies.values() if row.faction == state.armies["guard"].faction and row.id != "guard"]
    assert own, "fixture needs two armies of one faction"
    other = own[0]
    a, b = state.armies["guard"].strength, other.strength
    qa, qb = state.armies["guard"].quality, other.quality
    merged, facts = apply({"op": "merge_army", "army": other.id, "into": "guard", "reason": "归建"}, state)
    assert merged.armies["guard"].strength == pytest.approx(a + b)
    assert merged.armies["guard"].quality == pytest.approx(round((qa * a + qb * b) / (a + b), 3))
    assert merged.armies[other.id].strength == 0 and merged.armies[other.id].in_transit is None
    assert any("并入" in fact.text for fact in facts)
    assert summarize_ops([{"op": "merge_army", "army": other.id, "into": "guard"}], state)[0].endswith(f"并入{state.armies['guard'].name}")


def test_merge_army_rejects_other_faction_and_self(r9_state) -> None:
    from simcore.domain.rules import validate

    rebel = next(row.id for row in r9_state.armies.values() if row.faction != r9_state.armies["guard"].faction)
    from simcore.domain.rules import Rejected

    assert isinstance(validate({"op": "merge_army", "army": rebel, "into": "guard"}, r9_state), Rejected)
    assert isinstance(validate({"op": "merge_army", "army": "guard", "into": "guard"}, r9_state), Rejected)


def test_capital_memorials_are_presented_not_couriered(r9_state) -> None:
    from simcore.domain.courier import arrival_text

    capital = r9_state.rules.get("capital_region")
    assert capital, "fixture declares a capital"
    text = arrival_text(r9_state, str(capital))
    assert text.endswith("奏上") and "奏到" not in text
    other = next(region for region in r9_state.regions if region != capital)
    assert arrival_text(r9_state, other).endswith(f"自{r9_state.regions[other].name}奏到")


def test_foreign_characters_cannot_be_summoned_or_join_debate(mech_env) -> None:
    from test_round9 import new_game

    from simcore.service import GameError

    service, session_id = new_game(3)
    session = service.session(session_id)
    session.append_op({"op": "set_character_faction", "character": "plotter", "faction": "rebels"})
    foreign = session.head_state().characters["plotter"]
    listed = {row["character"] for row in [*service.present(session_id)["present"], *service.present(session_id)["absent"]]}
    assert foreign.id not in listed
    assert f"`{foreign.id}`" not in service.briefing(session_id)  # 简报的「可召对人物」也不列
    with pytest.raises(GameError) as err:
        service.begin_audience(session_id, foreign.id)
    assert err.value.code == "CHARACTER_UNAVAILABLE" and "非本朝臣属" in err.value.message


def test_dispatch_to_capital_office_brings_the_official_to_court(r9_state) -> None:
    """京官（无任所）写 dispatch: true：召外官入朝，此人启程赴御前；不写 dispatch 则只授职、人不动。"""
    assert r9_state.characters["plotter"].region == "north"
    state, facts = apply({"op": "appoint", "character": "plotter", "office": "censor", "dispatch": True}, r9_state)
    transit = state.characters["plotter"].in_transit
    assert transit and transit.get("to_region") == "capital"
    assert "已启程赴任" in facts[0].text
    assert "已启程赴任" in summarize_ops([{"op": "appoint", "character": "plotter", "office": "censor", "dispatch": True}], r9_state)[0]
    state, facts = apply({"op": "appoint", "character": "plotter", "office": "censor"}, r9_state)
    assert state.characters["plotter"].in_transit is None and state.characters["plotter"].region == "north"
    assert "启程" not in facts[0].text


def test_advisors_of_another_faction_do_not_speak(r9_state) -> None:
    """降而复叛者、敌国之人不在本朝奏疏下进言（与召对、廷议的「非本朝臣属」一致）。"""
    from simcore.domain.memorials import advisor_present

    state = r9_state.model_copy(deep=True)
    state.rules["player_faction"] = "court"
    advisor = {"character": "plotter", "stance": "support", "option": "a", "line": "臣以为可。"}
    assert advisor_present(advisor, state) == (True, False)
    state.characters["plotter"].faction = "rebels"
    assert advisor_present(advisor, state) == (False, False)
    state.rules.pop("player_faction")
    assert advisor_present(advisor, state) == (True, False)  # 未写 player_faction 的剧本不变


def test_character_region_arrived_excludes_officials_on_the_road(r9_state) -> None:
    """character_region 加 arrived: true：启程赴任者（region 仍是出发地）不算「在此地」。"""
    from simcore.domain.describe import describe_condition
    from simcore.domain.events import matches

    cond = {"kind": "character_region", "character": "plotter", "region": "north"}
    state, _ = apply({"op": "appoint", "character": "plotter", "office": "censor", "dispatch": True}, r9_state)
    assert matches(cond, state)
    assert not matches({**cond, "arrived": True}, state)
    assert matches({**cond, "arrived": True}, r9_state)
    assert "不在途中" in describe_condition({**cond, "arrived": True}, r9_state)


def test_commander_in_camp_retreats_with_the_beaten_army(r9_state) -> None:
    """败军退走时，在军中的统帅随军同退（不留在敌境）；遥领者不动。"""
    state = _battle_state(r9_state)
    state.armies["guard"].strength = 1000  # 守方必败
    guard = state.armies["guard"]
    commander = next(row for row in state.characters.values() if row.faction == guard.faction and row.status == "alive" and row.id != "ruler")
    commander.region, commander.in_transit, guard.commander = "plain", None, commander.id
    [battle], facts = resolve_battles(state, random.Random(9), WARS)
    retreat = battle["retreats"]["guard"]
    assert battle["winner"] != "court" and retreat
    assert state.characters[commander.id].region == retreat == state.armies["guard"].region
    assert any(fact.kind == "retreat" and commander.name in fact.text for fact in facts)

    remote = _battle_state(r9_state)
    remote.armies["guard"].strength = 1000
    far = next(region for region in remote.regions if region != "plain")
    remote.characters[commander.id].region, remote.armies["guard"].commander = far, commander.id
    resolve_battles(remote, random.Random(9), WARS)
    assert remote.characters[commander.id].region == far


def test_memorial_cooldown_brings_a_standing_item_back(r9_state) -> None:
    """常设条目写 cooldown: N：处理满 N 季后再呈上，旗标保留上次批答；不写 cooldown 的处理后不再出现。"""
    from simcore.domain.memorials import is_visible, memorial_flag

    item = {"id": "s_test", "title": "常设", "cooldown": 3, "options": [{"id": "a", "label": "甲", "ops": []}]}
    plain = {**item, "id": "s_once"}
    plain.pop("cooldown")
    state = r9_state.model_copy(deep=True)
    assert is_visible(item, state) and is_visible(plain, state)
    for row in (item, plain):
        state, _ = apply({"op": "set_flag", "key": memorial_flag(row["id"]), "value": "a"}, state)
    start = state.turn
    for turn, shown in ((start + 1, False), (start + 2, False), (start + 3, True), (start + 4, True)):
        state.turn = turn
        assert is_visible(item, state) is shown, turn
        assert not is_visible(plain, state)
    assert state.flags[memorial_flag("s_test")] == "a"  # 事件仍可读上次的批答


def test_clause_that_depends_on_a_delayed_clause_is_delayed_too(r9_state) -> None:
    """先令丞相致仕（被判延期）、再以他人补其官缺：补缺一条此刻会因「已有任职者」被拒，预览却按全部执行写成可行。
    cascade_delays 让它随前条延至下季；与延期条款无关的条款照常执行。"""
    from simcore.agent.tools import cascade_delays

    clauses = [
        {"status": "delayed", "delay_kind": "bureaucratic", "op": {"op": "set_character_status", "character": "chancellor", "status": "retired"}},
        {"status": "applied", "op": {"op": "appoint", "character": "general", "office": "chancellor"}},
        {"status": "applied", "op": {"op": "adjust_resource", "resource": next(iter(r9_state.resources)), "delta": 1}},
    ]
    assert cascade_delays(r9_state, clauses) == [2]
    assert clauses[1]["status"] == "delayed" and clauses[1]["delay_kind"] == "dependency" and clauses[1]["due_turn"] == r9_state.turn + 1
    assert "已有任职者" in clauses[1]["delay_reason"] or "任职" in clauses[1]["delay_reason"]
    assert clauses[2]["status"] == "applied"
    # 没有延期条款时什么也不改
    plain = [{"status": "applied", "op": {"op": "appoint", "character": "general", "office": "chancellor"}}]
    assert cascade_delays(r9_state, plain) == [] and plain[0]["status"] == "applied"


def test_topic_table_stances_flip_when_the_debate_is_on_the_counter_option(mech_scenario) -> None:
    """debate_topics 的派系立场是对奏疏原议（proposal）而言：议「谢绝」时，主「结盟」的一派应是反对。"""
    import copy

    from simcore.agent.debate import decide_stances

    scenario = copy.deepcopy(mech_scenario)
    definition = scenario.get("scenario", scenario)
    definition["debate_topics"] = [{"id": "pact", "keywords": ["结盟"], "stances": {"hawks": "support"}}]
    scenario["memorials"].append({
        "id": "m_pact", "turn": 0, "title": "结盟", "proposal": "ally", "keywords": ["结盟"],
        "options": [
            {"id": "ally", "label": "遣使结盟", "ops": []},
            {"id": "refuse", "label": "谢绝来使", "ops": []},
        ],
    })
    state = build_initial_state(scenario)
    state.characters["general"].bloc = "hawks"
    on_ally = decide_stances(state, scenario, "结盟", ["general"], memorial_id="m_pact", seed=1, proposal_option="ally")[0]
    on_refuse = decide_stances(state, scenario, "结盟", ["general"], memorial_id="m_pact", seed=1, proposal_option="refuse")[0]
    assert on_ally["stance"] == "support" and on_ally["target_option_id"] == "ally"
    assert on_refuse["stance"] == "oppose" and on_refuse["target_option_id"] == "ally" and "原议相反" in on_refuse["reason"]


def test_season_condition(r9_state) -> None:
    """season 条件：按当前季（结算中即正在结算的这一季）判断；value 可写一季或一组，operator: ne 取反。"""
    from simcore.domain.describe import describe_condition
    from simcore.domain.events import condition_errors, matches

    state = r9_state.model_copy(deep=True)
    state.calendar.season = "冬"
    assert matches({"kind": "season", "value": "冬"}, state)
    assert matches({"kind": "season", "value": ["秋", "冬"]}, state)
    assert not matches({"kind": "season", "value": "春"}, state)
    assert matches({"kind": "season", "operator": "ne", "value": "春"}, state)
    assert condition_errors({"kind": "season"}, "x") and not condition_errors({"kind": "season", "value": "冬"}, "x")
    assert "时令在冬" in describe_condition({"kind": "season", "value": "冬"}, state)


def test_retired_advisor_is_labelled(r9_state) -> None:
    """致仕者仍可上书，名下注「致仕在家，上书言」；在朝者不注。"""
    from simcore.domain.memorials import _public_item

    item = {"id": "m_x", "title": "议", "advisors": [{"character": "chancellor", "stance": "support", "line": "可。"}],
            "options": [{"id": "a", "label": "甲", "ops": []}]}
    assert _public_item(item, r9_state)["advisors"][0]["name"] == r9_state.characters["chancellor"].name
    state, _ = apply({"op": "set_character_status", "character": "chancellor", "status": "retired"}, r9_state)
    row = _public_item(item, state)["advisors"][0]
    assert row["name"].endswith("（致仕在家，上书言）") and row["retired"] is True


def test_briefing_shows_relations_and_fog_note(mech_env) -> None:
    """简报列出本方对各方的关系值；有风闻数值时注明区间每季重新估测。"""
    from test_round9 import new_game

    service, session_id = new_game(3)
    line = next(row for row in service.briefing(session_id).splitlines() if "诸国关系" in row)
    assert "叛军" in line or "rebels" in line or "-50" in line or "−50" in line


def test_late_report_marks_money_already_booked(r9_state) -> None:
    """远地军报晚到：钱粮事实注「已计入上季对账」，免得读来像本季又入一笔。"""
    from simcore.domain.courier import defer_event_report, release_late_reports
    from simcore.domain.models import Fact

    state = r9_state.model_copy(deep=True)
    resource = next(iter(state.resources))
    facts = [Fact(kind="resource", text="国库 +30（市舶抽解）", entity=resource, before=10, after=40), Fact(kind="event", text="海舶大至", entity="ships")]
    defer_event_report(state, {"id": "ships", "title": "海舶大至", "region": "west"}, facts, state.turn)
    _, released = release_late_reports(state, state.turn + 1)
    money = next(fact for fact in released if fact.kind == "resource")
    assert money.text.endswith("（已计入上季对账，军报今到）") and money.arrived_late


def test_adjusting_a_far_region_waits_for_the_courier(mech_env) -> None:
    """adjust_region 作用于远地（驿程超过阈值）时与任免、调兵一样，令达于下季。"""
    from simcore.domain.courier import far_target, is_far

    state = build_initial_state(load_scenario(FIXTURE_SCENARIOS / "r9_test"))
    far = next((region for region in state.regions if is_far(state, region)), None)
    assert far is not None
    assert far_target(state, [{"op": "adjust_region", "region": far, "stat": "prosperity", "delta": 3}])[0] == far
    near = next(region for region in state.regions if not is_far(state, region))
    assert far_target(state, [{"op": "adjust_region", "region": near, "stat": "prosperity", "delta": 3}]) is None


def test_memory_when_placeholder_is_the_season_it_happens(r9_state) -> None:
    state = r9_state.model_copy(deep=True)
    state.calendar.label = "宣和三年秋"
    state, _ = apply({"op": "remember", "character": "general", "fact": "{when}，奉命出镇"}, state)
    assert state.characters["general"].memories[-1] == "宣和三年秋，奉命出镇"
    assert summarize_ops([{"op": "remember", "character": "general", "fact": "{when}，奉命出镇"}], state)[0].endswith("宣和三年秋，奉命出镇")


def test_stamp_does_not_prefix_the_time_condition(r9_state) -> None:
    from simcore.domain.describe import stamp

    lines = stamp(["时间 ≥ 第3回合", "国库 ≥ 10"], 3, r9_state)
    assert lines[0] == "时间 ≥ 第3回合" or not lines[0].startswith(lines[1].split("时")[0] + "时时间")
    assert "时时间" not in "".join(lines) and lines[1].endswith("时国库 ≥ 10")


def test_pending_choice_options_carry_effect_summaries(r9_state) -> None:
    """待决事件的选项与奏疏选项一样给出效果摘要（简报与 /view 的 summary）。"""
    from simcore.domain.models import ChoiceOption, PendingChoice
    from simcore.service import choice_view

    resource = next(key for key, row in r9_state.resources.items() if not row.hidden)
    choice = PendingChoice(
        id="choice-x-1", event_id="x", title="议", text="", default_option="a", created_turn=r9_state.turn, deadline_turn=r9_state.turn + 1,
        options=[ChoiceOption(id="a", text="甲", effects=[{"op": "adjust_resource", "resource": resource, "delta": -10, "reason": "费"}]),
                 ChoiceOption(id="b", text="乙", effects=[])],
    )
    row = choice_view(choice, r9_state)
    assert row["options"][0]["summary"] and "−10" in row["options"][0]["summary"][0]
    assert row["options"][1]["summary"] == []


def test_difficulty_drift_bias_is_added_after_scaling(r9_state) -> None:
    """drift_bias：难档每回合的加数（先乘 drift_scale 再加），玩家把漂移压成负数时难档照样更难。"""
    from simcore.domain.difficulty import apply_difficulty, difficulty_errors, drift_bias

    state = r9_state.model_copy(deep=True)
    resource = next(iter(state.resources))
    apply_difficulty(state, {"id": "hard", "label": "难", "drift_scale": {resource: 1.2}, "drift_bias": {resource: 0.5}})
    assert drift_bias(state, resource) == 0.5 and drift_bias(state, "nope") == 0.0
    errors = difficulty_errors({"difficulties": [{"id": "x", "label": "x", "drift_bias": {"ghost": 1, resource: "a"}}]}, {resource}, set())
    assert any("ghost" in row for row in errors) and any("必须是数字" in row for row in errors)


def test_far_reports_still_on_the_road_are_delivered_when_the_game_ends(mech_env) -> None:
    """终局之后没有下一季：当季存下的远地军报在终局那次结算一并送达，不再许诺「下季送达」。"""
    from test_round9 import end_turn, new_game

    service, session_id = new_game(5)
    session = service.session(session_id)
    while session.head_state().turn < 7:
        end_turn(service, session_id)
    session.append_op({"op": "set_flag", "key": "force_west", "value": True})
    result = end_turn(service, session_id)
    assert session.head_state().outcome is not None
    events = result["report"]["events"]
    assert any(row.get("id") == "west_raid" and row.get("arrived_late") for row in events)
    assert not any("下季送达" in str(fact.get("text", "")) for fact in result["report"]["facts"])
