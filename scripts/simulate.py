"""Seeded balance simulation for any scenario pack (scenario-agnostic).

Plays N seeds of a scenario with simple, scripted policies and prints the ending
distribution, how long games last, the crisis gauge at the end and objective rates.
It only uses memorial options and event choices -- the decisions the scenario itself
offers -- so it measures the *content*, not a clever AI.

    cd backend
    python ../scripts/simulate.py ../scenarios/_template --seeds 200
    python ../scripts/simulate.py _template --policy last --difficulty hard
    python ../scripts/simulate.py _template --policy first --seed 3 --trace
    python ../scripts/simulate.py _template --backend service --seeds 20   # slower, exact game rules
    python ../scripts/simulate.py _template --coverage                       # fewest memorials per season

Policies (``--policy``, default ``all``):
    none    never answer a memorial; pending choices fall back to their default option
    first   always take the first option of every memorial and every event choice
    last    always take the last (allowed) option
    random  pick uniformly at random (seeded per game)

Backends:
    engine  (default) applies adopted ops directly and calls resolve_turn -- fast, enough for
            ending distributions. Skips edict delays (bureaucratic / courier / commander autonomy).
    service drives the same GameService the REST API and MCP server use (draft -> confirm ->
            choose -> end_turn), including all delays. Saves go to a temporary LLM_SIM_HOME.

For a scenario-specific harness with named strategies (a "wise" player, a "historical"
player, probes aimed at each ending) see tianbao's tools/sim_policies.py; this script is the
generic starting point.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from simcore.domain.apply import apply  # noqa: E402
from simcore.domain.events import matches  # noqa: E402
from simcore.domain.memorials import MemorialError, adopted_ops, is_visible, memorial_flag  # noqa: E402
from simcore.domain.models import WorldState  # noqa: E402
from simcore.domain.turn import GameOver, resolve_turn  # noqa: E402
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario  # noqa: E402

Policy = Callable[[list[str], random.Random], str | None]


def _first(options: list[str], rng: random.Random) -> str | None:
    return options[0] if options else None


def _last(options: list[str], rng: random.Random) -> str | None:
    return options[-1] if options else None


def _random(options: list[str], rng: random.Random) -> str | None:
    return rng.choice(options) if options else None


def _none(options: list[str], rng: random.Random) -> str | None:
    return None


# 逐季轨迹里额外打印的事实种类（战斗、行军、易主……）
TRACE_FACT_KINDS = frozenset({"battle", "standoff", "retreat", "region_controller", "movement", "arrival", "ai_hold", "autonomy"})

POLICIES: dict[str, Policy] = {"none": _none, "first": _first, "last": _last, "random": _random}


def resolve_pack(target: str) -> Path:
    path = Path(target)
    if not path.is_dir():
        path = ROOT / "scenarios" / target
    if not (path / "scenario.yaml").is_file():
        raise SystemExit(f"找不到场景：{target}（应为场景目录，或 scenarios/ 下的目录名）")
    return path.resolve()


def visible_memorials(scenario: dict[str, Any], state: WorldState) -> list[dict[str, Any]]:
    return [item for item in scenario.get("memorials") or [] if isinstance(item, dict) and is_visible(item, state)]


def allowed_choice_options(choice: Any, state: WorldState) -> list[str]:
    return [option.id for option in choice.options if matches(getattr(option, "conditions", None), state)]


def crisis_value(scenario: dict[str, Any], state: WorldState) -> float | None:
    crisis = scenario["scenario"].get("crisis") or {}
    resource = state.resources.get(str(crisis.get("resource") or ""))
    return resource.amount if resource else None


def objective_rows(scenario: dict[str, Any], state: WorldState) -> dict[str, bool]:
    rows: dict[str, bool] = {}
    for objective in scenario["scenario"].get("objectives") or []:
        failed = objective.get("fail_conditions") not in (None, {}, []) and matches(objective.get("fail_conditions"), state)
        reached = objective.get("conditions") not in (None, {}, []) and matches(objective.get("conditions"), state)
        rows[str(objective.get("id"))] = bool(reached and not failed)
    return rows


def describe(scenario: dict[str, Any], state: WorldState) -> str:
    parts = []
    for resource in state.resources.values():
        flag = "（隐）" if resource.hidden else ""
        parts.append(f"{resource.name}{flag} {resource.amount:.1f}")
    return "；".join(parts)


# ---------------------------------------------------------------------------------------------
# engine backend: adopted ops are applied directly (no delay rolls), then resolve_turn.
# ---------------------------------------------------------------------------------------------
def play_engine(scenario: dict[str, Any], policy: Policy, seed: int, difficulty: str | None, max_turns: int,
                trace: bool = False, on_turn: Callable[[int, list[dict[str, Any]]], None] | None = None) -> dict[str, Any]:
    state = build_initial_state(scenario, difficulty)
    rng = random.Random(f"policy:{seed}")
    peak = crisis_value(scenario, state)
    while state.outcome is None and state.turn < max_turns:
        visible = visible_memorials(scenario, state)
        if on_turn:
            on_turn(state.turn, visible)
        if trace:
            print(f"T{state.turn:>2} {state.calendar.label}：{describe(scenario, state)}")
        for item in visible:
            if not is_visible(item, state):  # an earlier answer this season may have closed it
                continue
            options = [str(option.get("id")) for option in item.get("options") or []]
            picked = policy(options, rng)
            if picked is None:
                continue
            try:
                ops = adopted_ops(scenario, state, str(item["id"]), picked)
            except MemorialError as exc:
                if trace:
                    print(f"     跳过奏疏 {item['id']}：{exc.message}")
                continue
            for op in ops:
                try:
                    state, _ = apply(op, state, source=str(item.get("title") or item["id"]))
                except ValueError as exc:
                    if trace:
                        print(f"     拒绝 {item['id']}/{picked} {op.get('op')}：{exc}")
            state, _ = apply({"op": "set_flag", "key": memorial_flag(str(item["id"])), "value": picked}, state)
            if trace:
                print(f"     奏疏「{item.get('title')}」→ {picked}")
        for choice in list(state.pending_choices):
            options = allowed_choice_options(choice, state)
            picked = policy(options, rng)
            if picked is None:
                continue
            try:
                state, _ = apply({"op": "choose_event_option", "choice_id": choice.id, "option_id": picked}, state)
                if trace:
                    print(f"     抉择「{choice.title}」→ {picked}")
            except ValueError as exc:
                if trace:
                    print(f"     抉择失败 {choice.id}：{exc}")
        try:
            state, report = resolve_turn(state, random.Random(seed + state.turn), scenario)
        except GameOver:
            break
        if trace:
            for event in report.events_fired:
                print(f"     事件：{event}")
            for fact in report.facts:
                if fact.kind in TRACE_FACT_KINDS:
                    print(f"     {fact.text}")
            for warning in report.warnings:
                print(f"     ! {warning}")
        value = crisis_value(scenario, state)
        if value is not None and peak is not None:
            peak = max(peak, value)
    if trace:
        print(f"终局：{state.outcome.title if state.outcome else '（未终局）'}，T{state.turn}")
    return summarize(scenario, state, peak)


# ---------------------------------------------------------------------------------------------
# service backend: the real draft -> confirm -> choose -> end_turn flow (delays included).
# ---------------------------------------------------------------------------------------------
async def _play_service_async(pack: Path, policy: Policy, seed: int, difficulty: str | None, max_turns: int,
                              trace: bool) -> dict[str, Any]:
    from simcore.service import GameError, GameService

    service = GameService()
    session_id, session = service.create_session(pack.name, seed, difficulty)
    scenario = session.scenario
    rng = random.Random(f"policy:{seed}")
    peak = crisis_value(scenario, session.state)
    while session.state.outcome is None and session.state.turn < max_turns:
        state = session.state
        if trace:
            print(f"T{state.turn:>2} {state.calendar.label}：{describe(scenario, state)}")
        adopted = []
        for item in visible_memorials(scenario, state):
            picked = policy([str(option.get("id")) for option in item.get("options") or []], rng)
            if picked is not None:
                adopted.append({"memorial_id": str(item["id"]), "option_id": picked})
        if adopted:
            receipt = await service.draft_edict(session_id, adopted=adopted, use_provider=False)
            service.confirm_edict(session_id, receipt["id"])
            if trace:
                for clause in receipt["clauses"]:
                    print(f"     [{clause.get('status')}] {clause.get('summary') or clause.get('text')}")
        for choice in list(session.state.pending_choices):
            for option_id in [policy(allowed_choice_options(choice, session.state), rng)]:
                if option_id is None:
                    continue
                try:
                    service.choose(session_id, choice.id, option_id)
                except GameError as exc:
                    if trace:
                        print(f"     抉择失败 {choice.id}：{exc.message}")
        try:
            result = await service.end_turn(session_id, narrate=False)
        except GameError as exc:
            if exc.code == "GAME_OVER":
                break
            raise
        if trace:
            for event in (result.get("report") or {}).get("events_fired", []):
                print(f"     事件：{event}")
        value = crisis_value(scenario, session.state)
        if value is not None and peak is not None:
            peak = max(peak, value)
    if trace:
        outcome = session.state.outcome
        print(f"终局：{outcome.title if outcome else '（未终局）'}，T{session.state.turn}")
    return summarize(scenario, session.state, peak)


def play_service(pack: Path, policy: Policy, seed: int, difficulty: str | None, max_turns: int, trace: bool = False) -> dict[str, Any]:
    return asyncio.run(_play_service_async(pack, policy, seed, difficulty, max_turns, trace))


def summarize(scenario: dict[str, Any], state: WorldState, peak: float | None) -> dict[str, Any]:
    outcome = state.outcome
    return {
        "outcome": outcome.id if outcome else "none",
        "title": outcome.title if outcome else "（未终局）",
        "kind": outcome.kind if outcome else "-",
        "turn": state.turn,
        "crisis": crisis_value(scenario, state),
        "peak": peak,
        "objectives": objective_rows(scenario, state),
    }


def report(name: str, results: list[dict[str, Any]], backend: str, difficulty: str | None, crisis_title: str) -> None:
    total = len(results)
    print(f"\n== {name}（{total} 局，后端 {backend}，难度 {difficulty or '默认'}）")
    counts = Counter(row["outcome"] for row in results)
    by_id = {row["outcome"]: row for row in results}
    turns: dict[str, list[int]] = defaultdict(list)
    for row in results:
        turns[row["outcome"]].append(row["turn"])
    for outcome_id, count in counts.most_common():
        row = by_id[outcome_id]
        print(f"   {outcome_id:<20} {row['kind']:<6} {count:>5} {count / total:7.1%}   平均终局 T{statistics.mean(turns[outcome_id]):.1f}  {row['title']}")
    values = [row["crisis"] for row in results if row["crisis"] is not None]
    peaks = [row["peak"] for row in results if row["peak"] is not None]
    if values:
        print(f"   危机计「{crisis_title}」终局值：均值 {statistics.mean(values):.1f}，中位 {statistics.median(values):.1f}，"
              f"范围 {min(values):.1f}–{max(values):.1f}；峰值均值 {statistics.mean(peaks):.1f}")
    objective_ids = sorted({key for row in results for key in row["objectives"]})
    if objective_ids:
        rates = "，".join(f"{key} {sum(row['objectives'].get(key, False) for row in results) / total:.0%}" for key in objective_ids)
        print(f"   终局时目标达成：{rates}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seeded balance simulation for a scenario pack")
    parser.add_argument("scenario", nargs="?", default="_template", help="场景目录或 scenarios/ 下的目录名")
    parser.add_argument("--policy", choices=[*POLICIES, "all"], default="all")
    parser.add_argument("--seeds", type=int, default=100)
    parser.add_argument("--seed", type=int, default=None, help="只跑这一个种子")
    parser.add_argument("--difficulty", default=None, help="难度档 id（缺省为 default_difficulty）")
    parser.add_argument("--backend", choices=["engine", "service"], default="engine")
    parser.add_argument("--max-turns", type=int, default=200)
    parser.add_argument("--trace", action="store_true", help="逐季打印（配合 --seed）")
    parser.add_argument("--coverage", action="store_true", help="每季可见奏疏的最少条数（engine 后端，跨策略）")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    pack = resolve_pack(args.scenario)
    errors = validate_scenario(pack)
    if errors:
        print("场景未通过 sim validate：\n  " + "\n  ".join(errors[:20]))
        return 1
    scenario = load_scenario(pack)
    crisis_title = str((scenario["scenario"].get("crisis") or {}).get("title") or "")

    if args.coverage:
        fewest: dict[int, tuple[int, str]] = {}
        for name, policy in POLICIES.items():
            def note(turn: int, visible: list[dict[str, Any]], name: str = name) -> None:
                if turn not in fewest or len(visible) < fewest[turn][0]:
                    fewest[turn] = (len(visible), name)
            for seed in range(min(args.seeds, 30)):
                play_engine(scenario, policy, seed, args.difficulty, args.max_turns, on_turn=note)
        print("每季最少可见奏疏条数：", {turn: count for turn, (count, _) in sorted(fewest.items())})
        return 0

    if args.backend == "service":
        home = tempfile.mkdtemp(prefix="sim-home-")
        os.environ["LLM_SIM_HOME"] = home
        os.environ["SIM_SCENARIOS_DIR"] = str(pack.parent)
        os.environ.setdefault("SIM_PROVIDER", "mock")

    names = list(POLICIES) if args.policy == "all" else [args.policy]
    seeds = [args.seed] if args.seed is not None else list(range(args.seeds))
    for name in names:
        policy = POLICIES[name]
        if args.backend == "engine":
            results = [play_engine(scenario, policy, seed, args.difficulty, args.max_turns, args.trace) for seed in seeds]
        else:
            results = [play_service(pack, policy, seed, args.difficulty, args.max_turns, args.trace) for seed in seeds]
        report(name, results, args.backend, args.difficulty, crisis_title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
