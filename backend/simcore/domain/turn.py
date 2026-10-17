from __future__ import annotations

import os
import random
from typing import Any

from simcore.domain.apply import apply, edict_memories
from simcore.domain.battle import advance_ai_routes, resolve_battles
from simcore.domain.calendar import format_calendar, start_season_index
from simcore.domain.courier import defer_event_report, event_is_far, record_memorial_dispatches, release_late_reports
from simcore.domain.court import reset_agenda
from simcore.domain.crisis import crisis_report, evaluate_terms, formula_values
from simcore.domain.describe import describe_satisfied, stamp
from simcore.domain.difficulty import drift_bias, drift_factor, gravity_factor
from simcore.domain.events import matches
from simcore.domain.formulas import evaluate
from simcore.domain.military import posture_attrition, scorch_effects
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
from simcore.domain.ops import memory_ops, parse_op
from simcore.domain.permissions import status_target_check
from simcore.domain.rules import coalesce_postings, turn_label
from simcore.domain.visibility import format_number


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
    # 分段对账：各资源在每段结算前后的值（差值逐段相加恰等于期末 − 期初）
    marks: list[dict[str, float]] = [_amounts(updated)]
    # 各段结算前后的事实序号：危机变化与经常收支明细按段归类事实（默认处置 / 延期 / 常设令与工程 / 事件）。
    fact_marks: list[int] = [0]
    # 远地奏疏：上季已可见者记下发出之季（下季送达，见 domain.courier）
    record_memorial_dispatches(scenario, state, updated)

    # 1. 到期的待决事件执行默认项
    for choice in list(updated.pending_choices):
        if choice.deadline_turn > target:
            continue
        updated = _resolve_default_choice(updated, choice, target, report)
    marks.append(_amounts(updated))
    fact_marks.append(len(report.facts))

    # 2. 延期条款
    pending = [row for row in updated.pending_clauses if row.due_turn <= target]
    updated.pending_clauses = [row for row in updated.pending_clauses if row.due_turn > target]
    for clause in pending:
        label = clause.label or ("延期诏令" if clause.source == "edict" else clause.source)
        if clause.source == "autonomy":
            # 将领自主（拒命 / 改作他令 / 擅自出战）：结算时写成事实（见 domain.military）
            report.facts.append(Fact(kind="autonomy", text=label, cause=label))
        remembered = {str(row.get("character")) for row in clause.ops if isinstance(row, dict) and row.get("op") == "remember"}
        for op in clause.ops:
            memories = edict_memories(op, updated) if clause.source in {"edict", "autonomy"} else []
            memories = [row for row in memories if row["character"] not in remembered]  # 同组已有剧本记忆，不再一事两记
            blocked = status_target_check(updated, op) if clause.source in {"edict", "autonomy"} else None
            if blocked is not None:
                report.warnings.append(f"延期条款未能执行（原延至第 {clause.due_turn} 回合结算时执行）：{blocked.message}")
                continue
            try:
                updated, facts = apply(op, updated, turn=target, inplace=True, source=label)
                report.facts.extend(facts)
            except ValueError as exc:
                report.warnings.append(f"延期条款未能执行（原延至第 {clause.due_turn} 回合结算时执行）：{exc}")
                continue
            for memory in memories:
                updated, facts = apply(memory, updated, turn=target, inplace=True)
                report.facts.extend(facts)
    marks.append(_amounts(updated))
    fact_marks.append(len(report.facts))

    # 3. 常设命令
    for order in list(updated.standing_orders):
        if order.until_turn is not None and order.until_turn < target:
            updated.standing_orders.remove(order)
            continue
        for op in order.ops:
            try:
                updated, facts = apply(op, updated, turn=target, inplace=True, source=f"常设令：{order.text}")
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
            LedgerLine(
                name=entry.name, amount=_credit(resource, entry.amount), kind=entry.kind, resource=resource_id, reason=entry.reason, group=entry.name
            )
        )
        if entry.kind == "one_off":
            updated.ledger.remove(entry)
        elif entry.turns is not None:
            entry.turns -= 1
            if entry.turns <= 0:
                updated.ledger.remove(entry)

    # 4b. 养兵（formulas.upkeep：按当前兵额逐项计费，入经常收支）
    formulas = scenario_definition.get("formulas", {}) or {}
    _collect_upkeep(updated, formulas.get("upkeep") or [], report)

    # 5. 收入
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
                LedgerLine(name=f"{project.name}施工", amount=-cost, kind="project", resource=resource, reason="工程每回合支出", group=f"{project.name}施工")
            )
        project.progress += 1
        report.facts.append(Fact(kind="project_progress", text=f"{project.name}进度 {project.progress}/{project.turns}", entity=project.id))
        if project.progress >= project.turns:
            for raw_op in project.on_complete:
                try:
                    updated, facts = apply(parse_op(raw_op), updated, turn=target, inplace=True, source=f"{project.name}竣工")
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

    # 8. 军令姿态（坚壁清野：本地区指标下降、敌军补给受损）→ 战斗（种子 RNG 的派生流，不扰动事件掷骰序列）与 AI 行军
    wars = scenario_definition.get("wars") or []
    report.facts.extend(scorch_effects(updated, wars))
    report.facts.extend(posture_attrition(updated, wars))
    battle_rng = random.Random(f"battle:{rng.getstate()[1][:8]}:{target}")
    battles, battle_facts = resolve_battles(updated, battle_rng, wars, arrived)
    report.battles.extend(battles)
    report.facts.extend(battle_facts)
    route_facts, route_warnings = advance_ai_routes(updated, scenario_definition.get("ai_routes") or [], target, wars)
    report.facts.extend(route_facts)
    report.warnings.extend(route_warnings)

    fact_marks.append(len(report.facts))
    # 9. 漂移
    crisis = scenario_definition.get("crisis") or {}
    crisis_id = str(crisis.get("resource") or "") if crisis else ""
    crisis_drift: dict[str, Any] = {}
    for resource_id, expression in (formulas.get("drift", {}) or {}).items():
        if resource_id not in updated.resources:
            continue
        resource = updated.resources[resource_id]
        values = formula_values(updated, target)
        factor = drift_factor(updated, resource_id)
        try:
            base = evaluate(expression, values)
        except (ValueError, ZeroDivisionError) as exc:
            report.warnings.append(f"漂移公式无效：{resource_id}: {exc}")
            continue
        bias = drift_bias(updated, resource_id)
        applied = _credit(resource, base * factor + bias)
        report.ledger_lines.append(
            LedgerLine(name=f"{resource.name}变动", amount=applied, kind="drift", resource=resource_id, reason="场景漂移公式", group=f"{resource.name}漂移")
        )
        if resource_id == crisis_id:
            crisis_drift = {
                "values": values,
                "terms": evaluate_terms(crisis, values),
                "base": base,
                "factor": factor,
                "difficulty": base * (factor - 1) + bias,
                "applied": applied,
                "clamp": applied - base * factor - bias,
            }
    marks.append(_amounts(updated))

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
    marks.append(_amounts(updated))
    fact_marks.append(len(report.facts))
    report.regular_detail = _regular_detail(report, fact_marks, marks)
    report.reconciliation = {
        key: {
            "delayed": marks[2][key] - marks[1][key],
            "regular": marks[3][key] - marks[2][key],
            "events": (marks[1][key] - marks[0][key]) + (marks[4][key] - marks[3][key]),
            "start": marks[0][key],
            "closing": marks[4][key],
        }
        for key in marks[0]
    }

    # 事件（第 10 步）里启程者出发在本回合移动抵达之后：行程从本回合起算，否则简报会写「预计本回合抵达」而人仍在途
    for entity_map in (updated.characters, updated.armies):
        for entity in entity_map.values():
            transit = entity.in_transit
            if transit and transit["eta_turn"] <= target:
                transit["eta_turn"] += target - state.turn

    # 12. 结局判定
    outcome = judge_outcome(updated, scenario_definition, target)
    if outcome is not None:
        updated.outcome = outcome
        report.outcome = outcome
        report.facts.append(Fact(kind="outcome", text=f"终局：{outcome.title}", entity=outcome.id))
        if updated.pending_choices:
            # 终局之后没有下一季可供处置：待决之事随局封存，不再列为「新的待决事件」，免得给出到不了的期限
            titles = "、".join(f"「{row.title}」" for row in updated.pending_choices)
            report.facts.append(Fact(kind="note", text=f"大局已定，待决之事{titles}随之封存，不再处置"))
            updated.pending_choices = []
            report.choices_opened = []
        ending = next(
            (row for row in scenario_definition.get("endings", []) or [] if isinstance(row, dict) and str(row.get("id")) == outcome.id),
            {},
        )
        report.events.append({
            "id": outcome.id,
            "title": outcome.title,
            "text": outcome.text,
            "illustration": ending.get("illustration") or None,
            "importance": 3,
            "internal": False,
            "outcome": True,
        })
    report.events = sort_events(report.events)

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
        # 危机计只在一切结算（延期条款、事件、抉择默认项）之后按最终状态计算一次：
        # end_turn、季报、/view 与 briefing 读到的是同一份数字。
        report.crisis = crisis_report(crisis, updated)
        if crisis_id in updated.resources:
            report.crisis["change"] = _crisis_change(report, crisis_id, fact_marks, marks, crisis_drift)
    # 远地军报：上季存下的事件条目与事实此时送达（带 arrived_late / origin_turn）；朝会日程计数清零
    late_events, late_facts = release_late_reports(updated, target)
    if updated.outcome is not None:
        # 终局之后没有下一季：本季存下的远地军报此刻一并送达，不再说「详情下季送达」
        final_events, final_facts = release_late_reports(updated, target + 1)
        for fact in final_facts:
            if fact.kind == "resource":
                fact.text = fact.text.replace("（已计入上季对账，军报今到）", "（已计入本季对账，终局时补报）")
        late_events.extend(final_events)
        late_facts.extend(final_facts)
        report.facts = [fact for fact in report.facts if not (fact.kind == "note" and fact.text.startswith(LATE_NOTE))]
    report.facts.extend(late_facts)
    if late_events:
        report.events = sort_events([*report.events, *late_events])
    reset_agenda(updated)
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
    gravity = min(1.0, float(event.get("gravity", 1)) * gravity_factor(state, event_id))
    if rng.random() > gravity:
        return state
    updated = state
    event_title = str(event.get("title", event_id))
    explicit_internal = event_is_internal(event)
    effect_facts: list[Fact] = []
    for op in coalesce_postings(list(event.get("effects", []) or []), updated):
        try:
            updated, facts = apply(op, updated, turn=target, inplace=True, source=event_title)
            effect_facts.extend(facts)
        except ValueError as exc:
            report.warnings.append(f"事件效果未执行：{event_title}: {exc}")
    raw_choices = event.get("choices") or []
    # 记账事件：显式 internal / kind: system，或既无正文、无抉择、也没有任何玩家可见的效果。
    internal = explicit_internal or (
        not str(event.get("text") or "").strip() and not raw_choices and all(fact.internal for fact in effect_facts)
    )
    if internal:
        for fact in effect_facts:
            if fact.kind in BOOKKEEPING_FACT_KINDS:
                fact.internal = True
    updated.flags[fired_key] = True
    updated.flag_turns[fired_key] = target
    report.events_fired.append(event_id)
    event_fact = Fact(kind="event", text=event_title, entity=event_id, internal=internal)
    entry = {
        "id": event_id,
        "title": event_title,
        "text": str(event.get("text") or ""),
        "illustration": event.get("illustration") or None,
        "importance": event_importance(event, effect_facts),
        "internal": internal,
    }
    if not internal and event_is_far(event, updated) and not event.get("choices"):
        # 远地事件：状态即时改变，军报（事件条目与可见事实）晚一季送达（见 domain.courier）。
        # 带抉择的远地事件不延：抉择此刻就摆到御前，事由须同时送到，不能先断后闻
        entry["region"] = str(event.get("region"))
        visible = [fact for fact in effect_facts if not fact.internal]
        report.facts.extend(fact for fact in effect_facts if fact.internal)
        defer_event_report(updated, entry, [*visible, event_fact], target)
        if any(fact.kind == "resource" for fact in visible):
            # 数目已在本季对账（事件/选择）里，详情随军报下季到：先说一声（一季一条，注明出事之地），免得对账里有数而事实里无据
            region = updated.regions.get(str(event.get("region")))
            where = region.name if region is not None else ""
            note = next((fact for fact in report.facts if fact.kind == "note" and fact.text.startswith(LATE_NOTE)), None)
            places = [*(note.entity.split("、") if note is not None and note.entity else []), *([where] if where else [])]
            places = list(dict.fromkeys(places))
            text = f"{LATE_NOTE}{'（' + '、'.join(places) + '）' if places else ''}：其钱粮等数目已计入本季对账「事件/选择」，详情下季送达"
            if note is None:
                report.facts.append(Fact(kind="note", text=text, entity="、".join(places) or None))
            else:
                note.text, note.entity = text, "、".join(places) or None
    else:
        report.facts.extend(effect_facts)
        report.facts.append(event_fact)
        report.events.append(entry)
    if raw_choices:
        options = [
            ChoiceOption(
                id=choice_option_id(row, index),
                text=str(row.get("text") or row.get("label") or choice_option_id(row, index)),
                effects=[*(row.get("effects") or row.get("ops") or []), *memory_ops(row.get("memory"))],
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


BOOKKEEPING_FACT_KINDS = frozenset({"ledger", "flag", "counter", "memory", "standing_order"})
WAR_OPS = frozenset({"move_army", "adjust_army", "merge_army", "set_army_faction", "set_army_commander", "set_region_controller", "set_character_faction"})
WAR_FACT_KINDS = frozenset({"battle", "army", "army_faction", "character_faction", "region_controller", "retreat"})
IMPORTANCE_LEVELS = {"outcome": 3, "war": 2, "battle": 2, "choice": 1, "normal": 0, "other": 0}


def event_is_internal(event: dict[str, Any]) -> bool:
    """剧本显式标记的记账事件：internal: true，或 kind: system。"""
    return event.get("internal") is True or str(event.get("kind", "")) == "system"


def event_importance(event: dict[str, Any], facts: list[Fact] | None = None) -> int:
    """事件的展示优先级：3 终局 > 2 兵事（战斗/调兵/易帜）> 1 带抉择 > 0 其他。剧本可用 importance 覆盖（数字或上述键名）。"""
    explicit = event.get("importance")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return int(explicit)
    if isinstance(explicit, str) and explicit in IMPORTANCE_LEVELS:
        return IMPORTANCE_LEVELS[explicit]
    tags = {str(item) for item in event.get("tags", []) or []} | {str(event.get("category", ""))}
    if tags & {"outcome", "ending"}:
        return 3
    ops = {str(op.get("op")) for op in event.get("effects", []) or [] if isinstance(op, dict)}
    if tags & {"war", "battle"} or ops & WAR_OPS or any(fact.kind in WAR_FACT_KINDS for fact in facts or []):
        return 2
    if event.get("choices"):
        return 1
    return 0


LATE_NOTE = "远地有事，军报未到"


def sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 importance 降序排列（同级保持触发顺序）。"""
    return sorted(events, key=lambda row: -int(row.get("importance", 0) or 0))


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
            satisfied_lines = stamp(describe_satisfied(ending["conditions"], state, turn), turn, state)
            return _outcome(ending, turn, satisfied_lines, extension_hint(ending, scenario_definition))
    length = scenario_definition.get("game_length") or {}
    if not length or length.get("last_turn") is None:
        return None
    last_turn = int(length["last_turn"])
    overtime_flag = length.get("overtime_flag")
    if overtime_flag and state.flags.get(overtime_flag) not in (None, False):
        last_turn = int(length.get("overtime_last_turn", last_turn))
    if turn < last_turn:
        return None
    satisfied = [f"{turn_label(state, turn)}已至终局时限，其他结局条件均未满足"]
    if last_turn != int(length["last_turn"]):
        flag = describe_satisfied({"kind": "flag", "key": overtime_flag, "value": True}, state, turn)
        satisfied.insert(0, f"{flag[0] if flag else overtime_flag}，时限由 {turn_label(state, int(length['last_turn']))} 延至 {turn_label(state, last_turn)}")
    fallback = length.get("fallback_ending")
    ending = next((row for row in endings if row.get("id") == fallback), None)
    if ending is not None:
        return _outcome(ending, turn, satisfied, extension_hint(ending, scenario_definition))
    return Outcome(kind="ending", id=str(fallback or "time_up"), title=str(fallback or "时限已至"), text="", turn=turn, satisfied=satisfied)


def final_season_notice(state: WorldState, scenario_definition: dict[str, Any]) -> dict[str, Any] | None:
    """下一次结算是否即终局（到达 game_length.last_turn；加时旗标已成立时按 overtime_last_turn）。

    返回 {next_is_final: True, last_turn, overtime_possible, text}；未到最后一季、已终局或场景无时限时为 None。
    其他结局条件可能更早终局，不在此列。
    """
    if state.outcome is not None:
        return None
    length = scenario_definition.get("game_length") or {}
    if not length or length.get("last_turn") is None:
        return None
    last_turn = int(length["last_turn"])
    overtime_flag = length.get("overtime_flag")
    overtime_on = bool(overtime_flag) and state.flags.get(str(overtime_flag)) not in (None, False)
    if overtime_on:
        last_turn = int(length.get("overtime_last_turn", last_turn))
    if state.turn + 1 < last_turn:
        return None
    text = (
        f"下次结算即终局：本季（{turn_label(state, state.turn, short=True)}）结算后即到终局时限 {turn_label(state, last_turn)}，"
        "此后不能再下诏、处置事件或推进回合；本季是最后的决策窗口。"
    )
    overtime_possible = bool(overtime_flag) and not overtime_on and length.get("overtime_last_turn") is not None
    if overtime_possible:
        labels = state.rules.get("flag_labels") or {}
        name = labels.get(str(overtime_flag), str(overtime_flag)) if isinstance(labels, dict) else str(overtime_flag)
        text += f"（唯若结算之中「{name}」成立，时限延至 {turn_label(state, int(length['overtime_last_turn']))}。）"
    return {"next_is_final": True, "last_turn": last_turn, "overtime_possible": overtime_possible, "text": text}


# 引擎已实现的「延长模式」（结局 extension_hint 只在剧本 game_length.extension_mode 属于其中时显示）；目前未实现任何一种。
SUPPORTED_EXTENSION_MODES: frozenset[str] = frozenset()


def extension_hint(ending: dict[str, Any], scenario_definition: dict[str, Any]) -> str:
    """结局的延长模式提示：剧本声明了引擎支持的 extension_mode 时才返回 extension_hint，否则为空串（目前恒为空）。"""
    mode = str((scenario_definition.get("game_length") or {}).get("extension_mode") or "")
    if not mode or mode not in SUPPORTED_EXTENSION_MODES:
        return ""
    return str(ending.get("extension_hint") or "")


def _outcome(ending: dict[str, Any], turn: int, satisfied: list[str] | None = None, hint: str = "") -> Outcome:
    kind = "fail" if ending.get("kind") == "fail" else "ending"
    return Outcome(
        kind=kind,
        id=str(ending["id"]),
        title=str(ending.get("title", ending["id"])),
        text=str(ending.get("text", "")) + (f"\n{hint}" if hint else ""),
        turn=turn,
        satisfied=list(satisfied or []),
    )


STAGE_KINDS = ("choice", "delayed", "standing", "event")  # fact_marks 的四段：默认处置、延期执行、常设令与工程、事件


def _resource_facts(report: TurnReport, resource_id: str, fact_marks: list[int]) -> list[dict[str, Any]]:
    """本季某资源的数值事实（明细）：[{kind, label, amount}]，kind 按所在结算段（choice / delayed / standing / event）。"""
    rows: list[dict[str, Any]] = []
    for stage, kind in enumerate(STAGE_KINDS):
        start, end = fact_marks[stage], fact_marks[stage + 1]
        for fact in report.facts[start:end]:
            if fact.kind != "resource" or fact.entity != resource_id:
                continue
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (fact.before, fact.after)):
                continue
            amount = float(fact.after) - float(fact.before)
            if amount:
                rows.append({"kind": kind, "label": fact.cause or fact.text, "amount": amount})
    return rows


def _merge_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同类同名的明细合并（诸道税入按收入公式 label 归一行）。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["kind"]), str(row["label"]))
        if key in merged:
            merged[key]["amount"] += row["amount"]
        else:
            merged[key] = dict(row)
    return list(merged.values())


def _regular_detail(report: TurnReport, fact_marks: list[int], marks: list[dict[str, float]]) -> dict[str, list[dict[str, Any]]]:
    """经常收支（第 3–9 步）明细：收入（按公式 label）、账目（逐项）、工程施工（逐项）、常设令与竣工效果、漂移；
    与该段总额的差（上下限截断等）记为「其他」。各项之和恰为该段总额。"""
    detail: dict[str, list[dict[str, Any]]] = {}
    for resource_id in marks[0]:
        total = marks[3][resource_id] - marks[2][resource_id]
        rows: list[dict[str, Any]] = []
        for line in report.ledger_lines:
            if line.resource != resource_id or not line.amount:
                continue
            kind = {"one_off": "ledger", "recurring": "ledger"}.get(line.kind, line.kind)
            rows.append({"kind": kind, "label": line.group or line.name, "amount": float(line.amount)})
        rows.extend({**row, "kind": "standing"} for row in _resource_facts(report, resource_id, fact_marks) if row["kind"] == "standing")
        rows = _merge_items(rows)
        residual = total - sum(row["amount"] for row in rows)
        if abs(residual) > 1e-9:
            rows.append({"kind": "other", "label": "其他（上下限截断）", "amount": residual})
        if rows:
            detail[resource_id] = rows
    return detail


def _crisis_change(
    report: TurnReport, resource_id: str, fact_marks: list[int], marks: list[dict[str, float]], drift: dict[str, Any]
) -> dict[str, Any]:
    """本季危机值变化的拆分（引擎内部真值；公开版见 visibility.public_crisis_change）。

    items：默认处置 / 延期执行 / 常设令与工程 / 事件冲击（带事件标题）与经常收支中的账目；drift：结算当时的分项、
    漂移基数、难度系数与加成、上下限截断；other：未归类的余差。诏令段（两次结算之间）由 SessionStore 补入。
    """
    items = _resource_facts(report, resource_id, fact_marks)
    items.extend(
        {"kind": "standing", "label": line.group or line.name, "amount": float(line.amount)}
        for line in report.ledger_lines
        if line.resource == resource_id and line.kind != "drift" and line.amount
    )
    items = _merge_items(items)
    opening, closing = marks[0][resource_id], marks[4][resource_id]
    applied = float(drift.get("applied", 0.0)) if drift else 0.0
    other = closing - opening - applied - sum(row["amount"] for row in items)
    return {
        "resource": resource_id,
        "opening": opening,
        "closing": closing,
        "items": items,
        "drift": drift,
        "other": other if abs(other) > 1e-9 else 0.0,
    }


def _amounts(state: WorldState) -> dict[str, float]:
    return {key: row.amount for key, row in state.resources.items()}


def upkeep_rows(state: WorldState, rules: Any) -> list[dict[str, Any]]:
    """formulas.upkeep 的逐项计算（不改状态）：[{label, resource, strength, per, amount, cost, armies}]。

    每项 `{resource, per, amount, label, factions?, armies?}`：所列势力（缺省为玩家势力；无玩家势力时为全部）
    或所列军队的现有兵力合计 strength，每 per 兵计 amount，本季开支 cost = strength / per × amount（正数为支出）。
    格式不对的条目跳过（validate_scenario 会报告）。
    """
    rows: list[dict[str, Any]] = []
    own = state.rules.get("player_faction")
    for index, row in enumerate(rules if isinstance(rules, list) else []):
        if not isinstance(row, dict):
            continue
        try:
            per = float(row.get("per", 0) or 0)
            amount = float(row.get("amount", 0) or 0)
        except (TypeError, ValueError):
            continue
        resource_id = str(row.get("resource") or "treasury")
        if per <= 0 or resource_id not in state.resources:
            continue
        factions = {str(item) for item in row.get("factions") or []} or ({str(own)} if own else set())
        wanted = {str(item) for item in row.get("armies") or []}
        armies = [
            army for army in state.armies.values()
            if army.strength > 0 and (army.id in wanted if wanted else (not factions or army.faction in factions))
        ]
        strength = sum(army.strength for army in armies)
        rows.append({
            "label": str(row.get("label") or f"养兵{index + 1}"),
            "resource": resource_id,
            "strength": strength,
            "per": per,
            "amount": amount,
            "cost": strength / per * amount,
            "armies": [army.id for army in armies],
        })
    return rows


def upkeep_label(row: dict[str, Any]) -> str:
    """明细名：「边军衣粮（兵 106000，每 10000 兵 15）」。"""
    return f"{row['label']}（兵 {format_number(row['strength'])}，每 {format_number(row['per'])} 兵 {format_number(row['amount'])}）"


def _collect_upkeep(state: WorldState, rules: Any, report: TurnReport) -> None:
    for row in upkeep_rows(state, rules):
        if not row["cost"]:
            continue
        resource = state.resources[row["resource"]]
        label = upkeep_label(row)
        report.ledger_lines.append(
            LedgerLine(name=label, amount=_credit(resource, -row["cost"]), kind="upkeep", resource=row["resource"], reason="按现兵额计", group=label)
        )


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
                    LedgerLine(
                        name=f"{region.name}{label}", amount=_credit(resource, amount), kind="income", resource=resource_id, reason="地区收入公式", group=label
                    )
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
                LedgerLine(name=label, amount=_credit(resource, amount), kind="income", resource=resource_id, reason="全国收入公式", group=label)
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
