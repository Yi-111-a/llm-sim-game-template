import random

from simcore.domain.apply import apply
from simcore.domain.models import (
    Calendar,
    Character,
    Office,
    Region,
    Resource,
    WorldState,
)
from simcore.domain.rules import Rejected, validate
from simcore.domain.turn import resolve_turn
from simcore.store.session import SessionStore, state_hash


def make_state() -> WorldState:
    return WorldState(
        scenario_id="test",
        turn=0,
        calendar=Calendar(year=1627, month=12, season="冬"),
        resources={
            "treasury": Resource(id="treasury", name="国库", amount=100),
            "grain": Resource(id="grain", name="粮储", amount=20),
            "authority": Resource(id="authority", name="权威", amount=50),
        },
        regions={
            "capital": Region(id="capital", name="京师", adjacency={"border": 2}, stats={"prosperity": 10}),
            "border": Region(id="border", name="边地", adjacency={"capital": 2}, stats={"prosperity": 4}),
        },
        characters={
            "minister": Character(id="minister", name="张某", region="capital", traits={"ability": 5}),
        },
        offices={
            "governor": Office(id="governor", name="巡抚", requires={"ability": 3}),
        },
    )


def test_one_off_and_recurring_ledger_settle_exactly_their_term() -> None:
    state = make_state()
    state, _ = apply(
        {"op": "add_ledger", "name": "一次拨款", "category": "treasury", "amount": 5, "kind": "one_off"},
        state,
    )
    state, _ = apply(
        {
            "op": "add_ledger",
            "name": "两回合俸支",
            "category": "treasury",
            "amount": -2,
            "kind": "recurring",
            "turns": 2,
        },
        state,
    )
    state, _ = resolve_turn(state, random.Random(7))
    assert state.resources["treasury"].amount == 103
    assert [entry.name for entry in state.ledger] == ["两回合俸支"]
    state, _ = resolve_turn(state, random.Random(7))
    assert state.resources["treasury"].amount == 101
    assert state.ledger == []
    state, _ = resolve_turn(state, random.Random(7))
    assert state.resources["treasury"].amount == 101


def test_ledger_can_be_cancelled() -> None:
    state = make_state()
    state, _ = apply(
        {
            "op": "add_ledger",
            "name": "军费",
            "category": "treasury",
            "amount": -4,
            "kind": "recurring",
            "turns": None,
        },
        state,
    )
    ledger_id = state.ledger[0].id
    state, _ = apply({"op": "cancel_ledger", "ledger_id": ledger_id}, state)
    state, _ = resolve_turn(state, random.Random(1))
    assert state.resources["treasury"].amount == 100


def test_dead_character_cannot_be_appointed_and_appointment_has_one_owner() -> None:
    state = make_state()
    state, _ = apply(
        {"op": "set_character_status", "character": "minister", "status": "dead", "cause": "病逝"},
        state,
    )
    result = validate({"op": "appoint", "character": "minister", "office": "governor"}, state)
    assert isinstance(result, Rejected)
    assert result.code == "CHARACTER_UNAVAILABLE"
    state = make_state()
    state, _ = apply({"op": "appoint", "character": "minister", "office": "governor"}, state)
    assert state.characters["minister"].office == "governor"
    assert state.offices["governor"].holder == "minister"


def test_project_charges_each_turn_and_applies_completion_ops() -> None:
    state = make_state()
    state, _ = apply(
        {
            "op": "start_project",
            "name": "修缮城垣",
            "region": "capital",
            "cost_per_turn": {"treasury": 10},
            "turns": 2,
            "on_complete": [{"op": "adjust_region", "region": "capital", "stat": "prosperity", "delta": 5}],
        },
        state,
    )
    state, _ = resolve_turn(state, random.Random(1))
    assert state.resources["treasury"].amount == 90
    assert state.projects[0].progress == 1
    state, _ = resolve_turn(state, random.Random(1))
    assert state.resources["treasury"].amount == 80
    assert state.projects == []
    assert state.regions["capital"].stats["prosperity"] == 15


def test_project_stalls_when_a_payment_cannot_be_made() -> None:
    state = make_state()
    state.resources["treasury"].amount = 5
    state, _ = apply(
        {
            "op": "start_project",
            "name": "小工程",
            "region": "capital",
            "cost_per_turn": {"treasury": 4},
            "turns": 2,
        },
        state,
    )
    state.resources["treasury"].amount = 3
    state, report = resolve_turn(state, random.Random(1))
    assert state.projects[0].progress == 0
    assert state.projects[0].stalled is True
    assert any("资金不足" in warning for warning in report.warnings)


def test_movement_uses_travel_turns_and_transit_blocks_appointment() -> None:
    state = make_state()
    state, _ = apply(
        {"op": "move_character", "character": "minister", "to_region": "border"},
        state,
    )
    assert state.characters["minister"].in_transit == {"to_region": "border", "eta_turn": 2}
    rejected = validate({"op": "appoint", "character": "minister", "office": "governor"}, state)
    assert isinstance(rejected, Rejected)
    assert rejected.code == "CHARACTER_UNAVAILABLE"
    state, _ = resolve_turn(state, random.Random(1))
    assert state.characters["minister"].in_transit is not None
    state, _ = resolve_turn(state, random.Random(1))
    assert state.characters["minister"].region == "border"
    assert state.characters["minister"].in_transit is None


def test_history_preconditions_and_seeded_gravity_are_respected() -> None:
    state = make_state()
    events = [
        {
            "id": "drought",
            "kind": "conditional",
            "conditions": {"kind": "resource", "id": "grain", "operator": "lte", "value": 10},
            "gravity": 1,
            "effects": [{"op": "set_flag", "key": "drought", "value": True}],
        },
        {
            "id": "eclipse",
            "kind": "random",
            "when": {"turn": 1},
            "gravity": 1,
            "effects": [{"op": "set_flag", "key": "eclipse", "value": True}],
        },
        {
            "id": "rare",
            "kind": "random",
            "when": {"turn": 1},
            "gravity": 0,
            "effects": [{"op": "set_flag", "key": "rare", "value": True}],
        },
    ]
    next_state, report = resolve_turn(state, random.Random(42), {"events": events})
    assert "drought" not in next_state.flags
    assert next_state.flags["eclipse"] is True
    assert "rare" not in next_state.flags
    assert report.events_fired == ["eclipse"]


def test_session_replay_hash_and_fork_divergence(tmp_path) -> None:
    scenario = {"turn_unit": "season", "events": []}
    session = SessionStore(tmp_path / "main.jsonl", scenario, make_state(), seed=13)
    first_state, _, first_entry = session.append_op(
        {"op": "add_ledger", "name": "拨付", "category": "treasury", "amount": -3, "kind": "one_off"}
    )
    session.append_turn()
    replayed, replay_hash = session.replay()
    assert replay_hash == state_hash(session.state)
    assert state_hash(replayed) == state_hash(session.state)
    fork = session.fork(tmp_path / "branch.jsonl", first_entry)
    fork.append_op({"op": "adjust_resource", "resource": "treasury", "delta": 20})
    assert fork.state.resources["treasury"].amount == first_state.resources["treasury"].amount + 20
    assert fork.state.turn == 0


def test_appointment_rejects_character_already_holding_another_office() -> None:
    state = make_state()
    state.offices["censor"] = Office(id="censor", name="御史")
    state, _ = apply({"op": "appoint", "character": "minister", "office": "governor"}, state)
    result = validate({"op": "appoint", "character": "minister", "office": "censor"}, state)
    assert isinstance(result, Rejected)
    assert result.code == "OFFICE_HELD"
    state, _ = apply({"op": "dismiss", "character": "minister", "office": "governor"}, state)
    state, _ = apply({"op": "appoint", "character": "minister", "office": "censor"}, state)
    assert state.offices["governor"].holder is None
    assert state.offices["censor"].holder == "minister"
    assert state.characters["minister"].office == "censor"


def test_new_project_must_fit_alongside_committed_project_costs() -> None:
    state = make_state()
    project = {
        "op": "start_project",
        "name": "修缮城垣",
        "region": "capital",
        "cost_per_turn": {"treasury": 60},
        "turns": 2,
    }
    state, _ = apply(project, state)
    result = validate({**project, "name": "疏浚河道"}, state)
    assert isinstance(result, Rejected)
    assert result.code == "INSUFFICIENT_RESOURCE"


def test_turn_condition_and_when_turn_refer_to_the_same_turn() -> None:
    state = make_state()
    events = [
        {"id": "by_when", "when": {"turn": 2}, "gravity": 1, "effects": [{"op": "set_flag", "key": "by_when", "value": True}]},
        {
            "id": "by_condition",
            "conditions": {"kind": "turn", "value": 2},
            "gravity": 1,
            "effects": [{"op": "set_flag", "key": "by_condition", "value": True}],
        },
    ]
    state, first = resolve_turn(state, random.Random(1), {"events": events})
    assert first.events_fired == []
    state, second = resolve_turn(state, random.Random(1), {"events": events})
    assert second.turn == 2
    assert sorted(second.events_fired) == ["by_condition", "by_when"]


def test_drift_and_tax_income_respect_bounds_and_appear_in_ledger() -> None:
    state = make_state()
    state.resources["morale"] = Resource(id="morale", name="民心", amount=0.1, minimum=0, maximum=100)
    state.resources["treasury"].maximum = 102
    scenario = {
        "formulas": {
            "income": {"tax": {"resource": "treasury", "formula": "prosperity"}},
            "drift": {"morale": "-1"},
        }
    }
    state, report = resolve_turn(state, random.Random(1), scenario)
    assert state.resources["morale"].amount == 0
    assert state.resources["treasury"].amount == 102
    drift_lines = [line for line in report.ledger_lines if line.kind == "drift"]
    assert [(line.resource, line.amount) for line in drift_lines] == [("morale", -0.1)]
    income = sum(line.amount for line in report.ledger_lines if line.kind == "income")
    assert income == 2
