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
