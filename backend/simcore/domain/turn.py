from __future__ import annotations

import random
from typing import Any

from simcore.domain.apply import apply
from simcore.domain.calendar import format_calendar
from simcore.domain.events import matches
from simcore.domain.formulas import evaluate
from simcore.domain.models import Fact, LedgerLine, Resource, TurnReport, WorldState
from simcore.domain.ops import parse_op


def resolve_turn(
    state: WorldState, rng: random.Random, scenario: dict[str, Any] | None = None
) -> tuple[WorldState, TurnReport]:
    scenario = scenario or {}
    scenario_definition = scenario.get("scenario", scenario)
    updated = state.model_copy(deep=True)
    report = TurnReport(turn=state.turn + 1)

    pending = [row for row in updated.pending_clauses if row.due_turn <= updated.turn + 1]
    updated.pending_clauses = [row for row in updated.pending_clauses if row.due_turn > updated.turn + 1]
    for clause in pending:
        for op in clause.ops:
            try:
                updated, facts = apply(op, updated)
                report.facts.extend(facts)
            except ValueError as exc:
                report.warnings.append(f"延期条款未执行：{exc}")

    for order in list(updated.standing_orders):
        if order.until_turn is not None and order.until_turn < updated.turn + 1:
            updated.standing_orders.remove(order)
            continue
        for op in order.ops:
            try:
                updated, facts = apply(op, updated)
                report.facts.extend(facts)
            except ValueError as exc:
                report.warnings.append(f"常设命令未执行：{exc}")

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

    formulas = scenario_definition.get("formulas", {})
    for region in updated.regions.values():
        income = formulas.get("income", {}).get("tax")
        if income:
            resource_id = income.get("resource", "treasury")
            resource = updated.resources.get(resource_id)
            if resource:
                values = {**region.stats, "turn": float(updated.turn + 1)}
                amount = _credit(resource, evaluate(income.get("formula", "0"), values))
                report.ledger_lines.append(
                    LedgerLine(name=f"{region.name}税入", amount=amount, kind="income", resource=resource_id, reason="地区税赋公式")
                )

    completed: list[Any] = []
    for project in updated.projects:
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
                    updated, facts = apply(parse_op(raw_op), updated)
                    report.facts.extend(facts)
                except ValueError as exc:
                    report.warnings.append(f"竣工效果未执行：{project.name}: {exc}")
            report.facts.append(Fact(kind="project_complete", text=f"工程竣工：{project.name}", entity=project.id))
            completed.append(project)
    for project in completed:
        updated.projects.remove(project)

    for entity_map in (updated.characters, updated.armies):
        for entity in entity_map.values():
            transit = entity.in_transit
            if transit and transit["eta_turn"] <= updated.turn + 1:
                before = entity.region
                entity.region = transit["to_region"]
                entity.in_transit = None
                report.facts.append(Fact(kind="arrival", text="抵达目的地", entity=entity.id, before=before, after=entity.region))

    drift = formulas.get("drift", {})
    for resource_id, expression in drift.items():
        if resource_id in updated.resources:
            resource = updated.resources[resource_id]
            amount = evaluate(expression, {key: row.amount for key, row in updated.resources.items()})
            report.ledger_lines.append(
                LedgerLine(name=f"{resource.name}变动", amount=_credit(resource, amount), kind="drift", resource=resource_id, reason="场景漂移公式")
            )

    for event in scenario.get("events", []):
        event_id = event["id"]
        if updated.flags.get(f"event:{event_id}:fired"):
            continue
        if not _scheduled(event, updated.turn + 1) or not matches(event.get("conditions"), updated, updated.turn + 1):
            continue
        gravity = float(event.get("gravity", 1))
        if rng.random() > gravity:
            continue
        for op in event.get("effects", []):
            try:
                updated, facts = apply(op, updated)
                report.facts.extend(facts)
            except ValueError as exc:
                report.warnings.append(f"事件效果未执行：{event_id}: {exc}")
        updated.flags[f"event:{event_id}:fired"] = True
        report.events_fired.append(event_id)
        report.facts.append(Fact(kind="event", text=event.get("title", event_id), entity=event_id))

    calendar_config = scenario_definition.get("calendar", {})
    _advance_calendar(updated, scenario_definition.get("turn_unit", "month"), calendar_config)
    updated.turn += 1
    if calendar_config:
        updated.calendar.label = format_calendar(
            updated.turn, updated.calendar.season, calendar_config
        )
    from simcore.store.session import state_hash

    report.turn = updated.turn
    report.state_hash = state_hash(updated)
    return updated, report


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
    when = event.get("when", {})
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
