from __future__ import annotations

import os
import random
from typing import Any

from simcore.domain.apply import apply
from simcore.domain.battle import advance_ai_routes, resolve_battles
from simcore.domain.calendar import format_calendar, start_season_index
from simcore.domain.crisis import crisis_report, evaluate_terms, formula_values
from simcore.domain.events import matches
from simcore.domain.formulas import evaluate
from simcore.domain.models import (
    ChoiceOption,
    Fact,
    LedgerLine,
    Outcome,
    PendingChoice,
    Resource,
    TurnReport,
    WorldState,
    clone,
)
from simcore.domain.ops import parse_op


class GameOver(RuntimeError):
    """本局已终局，不能再推进回合。"""

    code = "GAME_OVER"


def resolve_turn(
    state: WorldState, rng: random.Random, scenario: dict[str, Any] | None = None
) -> tuple[WorldState, TurnReport]:
    """结算一个回合。顺序见 ARCHITECTURE.md：

    1 到期抉择默认项 → 2 延期条款 → 3 常设命令 → 4 账目 → 5 收入 → 6 工程 → 7 移动抵达
    → 8 战斗与 AI 行军 → 9 漂移（危机分项）→ 10 事件 → 11 计数器 → 12 结局 → 13 历法。
    """
    if state.outcome is not None:
        raise GameOver(f"本局已终局：{state.outcome.title}")
    scenario = scenario or {}
    scenario_definition = scenario.get("scenario", scenario)
    target = state.turn + 1
    updated = clone(state)
    report = TurnReport(turn=target)

    # 1. 到期的待决事件执行默认项
    for choice in list(updated.pending_choices):
        if choice.deadline_turn > target:
            continue
        updated = _resolve_default_choice(updated, choice, target, report)

    # 2. 延期条款
    pending = [row for row in updated.pending_clauses if row.due_turn <= target]
    updated.pending_clauses = [row for row in updated.pending_clauses if row.due_turn > target]
    for clause in pending:
        for op in clause.ops:
            try:
                updated, facts = apply(op, updated, turn=target, inplace=True)
                report.facts.extend(facts)
            except ValueError as exc:
                report.warnings.append(f"延期条款未执行：{exc}")

    # 3. 常设命令
    for order in list(updated.standing_orders):
        if order.until_turn is not None and order.until_turn < target:
            updated.standing_orders.remove(order)
            continue
        for op in order.ops:
            try:
                updated, facts = apply(op, updated, turn=target, inplace=True)
                report.facts.extend(facts)
            except ValueError as exc:
                report.warnings.append(f"常设命令未执行：{exc}")

    # 4. 账目
    for entry in list(updated.ledger):
        resource_id = entry.category
        resource = updated.resources.get(resource_id)
        if resource is None:
            report.warnings.append(f"账目资源不存在：{resource_id}")
            updated.ledger.remove(entry)
            continue
        report.ledger_lines.append(
            LedgerLine(name=entry.name, amount=_credit(resource, entry.amount), kind=entry.kind, resource=resource_id, reason=entry.reason)
        )
        if entry.kind == "one_off":
            updated.ledger.remove(entry)
        elif entry.turns is not None:
            entry.turns -= 1
            if entry.turns <= 0:
                updated.ledger.remove(entry)

    # 5. 收入
    formulas = scenario_definition.get("formulas", {}) or {}
    _collect_income(updated, formulas.get("income", {}) or {}, target, report)

    # 6. 工程
    completed: list[Any] = []
    for project in list(updated.projects):
        if any(
            updated.resources.get(resource) is None
            or updated.resources[resource].amount < cost
            for resource, cost in project.cost_per_turn.items()
        ):
            project.stalled = True
            report.warnings.append(f"工程资金不足，暂停施工：{project.name}")
            continue
        project.stalled = False
        for resource, cost in project.cost_per_turn.items():
            updated.resources[resource].amount -= cost
            report.ledger_lines.append(
                LedgerLine(name=f"{project.name}施工", amount=-cost, kind="project", resource=resource, reason="工程每回合支出")
            )
        project.progress += 1
        report.facts.append(Fact(kind="project_progress", text=f"{project.name}进度 {project.progress}/{project.turns}", entity=project.id))
        if project.progress >= project.turns:
            for raw_op in project.on_complete:
                try:
                    updated, facts = apply(parse_op(raw_op), updated, turn=target, inplace=True)
                    report.facts.extend(facts)
                except ValueError as exc:
                    report.warnings.append(f"竣工效果未执行：{project.name}: {exc}")
            report.facts.append(Fact(kind="project_complete", text=f"工程竣工：{project.name}", entity=project.id))
            completed.append(project)
    for project in completed:
        if project in updated.projects:
            updated.projects.remove(project)

    # 7. 移动抵达
    arrived: set[str] = set()
    for entity_map in (updated.characters, updated.armies):
        for entity in entity_map.values():
            transit = entity.in_transit
            if transit and transit["eta_turn"] <= target:
                before = entity.region
                entity.region = transit["to_region"]
                entity.in_transit = None
                arrived.add(entity.id)
                destination = updated.regions[entity.region].name if entity.region in updated.regions else entity.region
                report.facts.append(Fact(kind="arrival", text=f"{entity.name}抵达{destination}", entity=entity.id, before=before, after=entity.region))

    # 8. 战斗（种子 RNG 的派生流，不扰动事件掷骰序列）与 AI 行军
    wars = scenario_definition.get("wars") or []
    battle_rng = random.Random(f"battle:{rng.getstate()[1][:8]}:{target}")
    battles, battle_facts = resolve_battles(updated, battle_rng, wars, arrived)
    report.battles.extend(battles)
    report.facts.extend(battle_facts)
    route_facts, route_warnings = advance_ai_routes(updated, scenario_definition.get("ai_routes") or [], target, wars)
    report.facts.extend(route_facts)
    report.warnings.extend(route_warnings)

    # 9. 漂移；危机分项与危机资源的漂移使用同一组变量快照
    crisis = scenario_definition.get("crisis") or {}
    crisis_terms: list[dict[str, Any]] | None = None
    for resource_id, expression in (formulas.get("drift", {}) or {}).items():
        if resource_id not in updated.resources:
            continue
        resource = updated.resources[resource_id]
        values = formula_values(updated, target)
        if crisis and resource_id == crisis.get("resource"):
            crisis_terms = evaluate_terms(crisis, values)
        try:
            amount = evaluate(expression, values)
        except (ValueError, ZeroDivisionError) as exc:
            report.warnings.append(f"漂移公式无效：{resource_id}: {exc}")
            continue
        report.ledger_lines.append(
            LedgerLine(name=f"{resource.name}变动", amount=_credit(resource, amount), kind="drift", resource=resource_id, reason="场景漂移公式")
        )
    if crisis and crisis_terms is None:
        crisis_terms = evaluate_terms(crisis, formula_values(updated, target))

    # 10. 事件
    for event in scenario.get("events", []) or []:
        updated = _maybe_fire_event(updated, event, target, rng, report)

    # 11. 计数器规则
    for counter in scenario_definition.get("counters", []) or []:
        key = str(counter.get("id"))
        if matches(counter.get("conditions"), updated, target):
            updated.counters[key] = updated.counters.get(key, 0) + 1
        elif counter.get("mode", "streak") == "streak":
            updated.counters[key] = 0

    # 12. 结局判定
    outcome = judge_outcome(updated, scenario_definition, target)
    if outcome is not None:
        updated.outcome = outcome
        report.outcome = outcome
        report.facts.append(Fact(kind="outcome", text=f"终局：{outcome.title}", entity=outcome.id))

    # 13. 历法
    calendar_config = scenario_definition.get("calendar", {})
    _advance_calendar(updated, scenario_definition.get("turn_unit", "month"), calendar_config)
    updated.turn = target
    if calendar_config:
        updated.calendar.label = format_calendar(
            updated.turn,
            updated.calendar.season,
            calendar_config,
            start_season_index(scenario_definition),
            fallback_year=updated.calendar.year,
        )
    if crisis:
        report.crisis = crisis_report(crisis, updated, crisis_terms)
    from simcore.store.session import state_hash

    if os.environ.get("SIM_CHECK_INVARIANTS"):
        from simcore.domain.invariants import check_invariants

        problems = check_invariants(updated, previous=state)
        if problems:
            raise AssertionError("; ".join(problems))
    report.turn = updated.turn
    report.state_hash = state_hash(updated)
    return updated, report


def _resolve_default_choice(state: WorldState, choice: PendingChoice, target: int, report: TurnReport) -> WorldState:
    ordered = sorted(choice.options, key=lambda option: option.id != choice.default_option)
    option = next((row for row in ordered if matches(row.conditions, state, target)), None)
    if option is None:
        state.pending_choices = [row for row in state.pending_choices if row.id != choice.id]
        report.warnings.append(f"「{choice.title}」逾期未决，且无可行选项")
        return state
    report.facts.append(Fact(kind="choice_default", text=f"「{choice.title}」逾期未决，依例处置：{option.text}", entity=choice.event_id, after=option.id))
    try:
        state, facts = apply({"op": "choose_event_option", "choice_id": choice.id, "option_id": option.id}, state, turn=target, inplace=True)
        report.facts.extend(facts)
    except ValueError as exc:
        state.pending_choices = [row for row in state.pending_choices if row.id != choice.id]
        report.warnings.append(f"默认选项未执行：{choice.title}: {exc}")
    return state


def _maybe_fire_event(state: WorldState, event: dict[str, Any], target: int, rng: random.Random, report: TurnReport) -> WorldState:
    event_id = str(event["id"])
    fired_key = f"event:{event_id}:fired"
    if state.flags.get(fired_key):
        if not event.get("repeatable", False):
            return state
        last = state.flag_turns.get(fired_key)
        cooldown = max(1, int(event.get("cooldown", 1) or 1))
        if last is not None and target - last < cooldown:
            return state
    if any(choice.event_id == event_id for choice in state.pending_choices):
        return state
    if not _scheduled(event, target) or not matches(event.get("conditions"), state, target):
        return state
    gravity = float(event.get("gravity", 1))
    if rng.random() > gravity:
        return state
    updated = state
    for op in event.get("effects", []) or []:
        try:
            updated, facts = apply(op, updated, turn=target, inplace=True)
            report.facts.extend(facts)
        except ValueError as exc:
            report.warnings.append(f"事件效果未执行：{event_id}: {exc}")
    updated.flags[fired_key] = True
    updated.flag_turns[fired_key] = target
    report.events_fired.append(event_id)
    report.facts.append(Fact(kind="event", text=event.get("title", event_id), entity=event_id))
    raw_choices = event.get("choices") or []
    if raw_choices:
        options = [
            ChoiceOption(
                id=choice_option_id(row, index),
                text=str(row.get("text") or row.get("label") or choice_option_id(row, index)),
                effects=list(row.get("effects") or row.get("ops") or []),
                conditions=row.get("conditions"),
            )
            for index, row in enumerate(raw_choices)
        ]
        default = str(event.get("default_choice") or options[0].id)
        pending = PendingChoice(
            id=f"choice-{event_id}-{target}",
            event_id=event_id,
            title=str(event.get("title", event_id)),
            text=str(event.get("text", "")),
            options=options,
            default_option=default,
            created_turn=target,
            deadline_turn=target + max(1, int(event.get("choice_deadline", 1) or 1)),
        )
        updated.pending_choices.append(pending)
        report.choices_opened.append(pending.model_dump(mode="json"))
    return updated


def choice_option_id(row: dict[str, Any], index: int) -> str:
    """选项 id；旧剧本未写 id 时按顺序编为 option_1、option_2……"""
    return str(row.get("id") or f"option_{index + 1}")


def judge_outcome(state: WorldState, scenario_definition: dict[str, Any], turn: int) -> Outcome | None:
    """按 priority 从小到大判定结局；无条件的结局只作兜底，不参与判定。"""
    endings = [row for row in scenario_definition.get("endings", []) or [] if isinstance(row, dict)]
    for ending in sorted(endings, key=lambda row: (float(row.get("priority", 100)), str(row.get("id")))):
        if ending.get("conditions") in (None, {}, []):
            continue
        if matches(ending["conditions"], state, turn):
            return _outcome(ending, turn)
    length = scenario_definition.get("game_length") or {}
    if not length or length.get("last_turn") is None:
        return None
    last_turn = int(length["last_turn"])
    overtime_flag = length.get("overtime_flag")
    if overtime_flag and state.flags.get(overtime_flag) not in (None, False):
        last_turn = int(length.get("overtime_last_turn", last_turn))
    if turn < last_turn:
        return None
    fallback = length.get("fallback_ending")
    ending = next((row for row in endings if row.get("id") == fallback), None)
    if ending is not None:
        return _outcome(ending, turn)
    return Outcome(kind="ending", id=str(fallback or "time_up"), title=str(fallback or "时限已至"), text="", turn=turn)


def _outcome(ending: dict[str, Any], turn: int) -> Outcome:
    kind = "fail" if ending.get("kind") == "fail" else "ending"
    return Outcome(kind=kind, id=str(ending["id"]), title=str(ending.get("title", ending["id"])), text=str(ending.get("text", "")), turn=turn)


def _collect_income(state: WorldState, incomes: dict[str, Any], target: int, report: TurnReport) -> None:
    main_faction = state.rules.get("player_faction")
    for key, income in incomes.items():
        if not isinstance(income, dict):
            continue
        resource_id = income.get("resource", "treasury")
        resource = state.resources.get(resource_id)
        if resource is None:
            report.warnings.append(f"收入资源不存在：{resource_id}")
            continue
        label = str(income.get("label") or ("税入" if key == "tax" else key))
        expression = income.get("formula", "0")
        if income.get("per_region", True):
            for region in state.regions.values():
                controlled = region.controller is None or main_faction is None or region.controller == main_faction
                if income.get("controlled_only", False) and not controlled:
                    continue
                values = {**formula_values(state, target), **region.stats, "turn": float(target), "controlled": 1.0 if controlled else 0.0}
                try:
                    amount = evaluate(expression, values)
                except (ValueError, ZeroDivisionError) as exc:
                    report.warnings.append(f"收入公式无效：{key}: {exc}")
                    break
                report.ledger_lines.append(
                    LedgerLine(name=f"{region.name}{label}", amount=_credit(resource, amount), kind="income", resource=resource_id, reason="地区收入公式")
                )
        else:
            values = {**formula_values(state, target), **{f"counter_{name}": float(value) for name, value in state.counters.items()}}
            values["controlled_regions"] = float(
                sum(1 for region in state.regions.values() if main_faction is None or region.controller in (None, main_faction))
            )
            try:
                amount = evaluate(expression, values)
            except (ValueError, ZeroDivisionError) as exc:
                report.warnings.append(f"收入公式无效：{key}: {exc}")
                continue
            report.ledger_lines.append(
                LedgerLine(name=label, amount=_credit(resource, amount), kind="income", resource=resource_id, reason="全国收入公式")
            )


def _credit(resource: Resource, amount: float) -> float:
    """按资源上下限入账，返回实际变动量。"""
    before = resource.amount
    resource.amount += amount
    if resource.minimum is not None:
        resource.amount = max(resource.minimum, resource.amount)
    if resource.maximum is not None:
        resource.amount = min(resource.maximum, resource.amount)
    return resource.amount - before


def _scheduled(event: dict[str, Any], turn: int) -> bool:
    when = event.get("when", {}) or {}
    return when.get("turn") in (None, turn)


def _advance_calendar(
    state: WorldState, unit: str, calendar_config: dict[str, Any] | None = None
) -> None:
    calendar_config = calendar_config or {}
    if unit == "month":
        state.calendar.month += 1
        if state.calendar.month > 12:
            state.calendar.month = 1
            state.calendar.year += 1
        seasons = calendar_config.get("seasons", ["春", "夏", "秋", "冬"])
        season_index = min(len(seasons) - 1, (state.calendar.month - 1) * len(seasons) // 12)
        state.calendar.season = seasons[season_index]
    elif unit == "season":
        order = calendar_config.get("seasons", ["春", "夏", "秋", "冬"])
        index = order.index(state.calendar.season) if state.calendar.season in order else 0
        if index == len(order) - 1:
            state.calendar.year += 1
        state.calendar.season = order[(index + 1) % len(order)]
        state.calendar.month = (order.index(state.calendar.season) * 3) + 1
    elif unit == "year":
        state.calendar.year += 1
    state.calendar.label = f"{state.calendar.year}年{state.calendar.season}"
