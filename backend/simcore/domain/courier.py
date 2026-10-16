"""驿传：奏到与令达（第 9 轮，CONTRACT §7.3）。

- 地区 `courier_days`（驿程日数，剧本可写）；未写时按到京城的行程季数估算：0 季 → 3 日，1 季 → 15 日，2 季 → 40 日，
  3 季及以上（或无路可达）→ 70 日。
- 阈值 `settings.courier.late_threshold_days`（缺省 60）：驿程超过阈值的地区为「远地」——
  其奏疏与该地区事件的军报晚一季才出现；诏令中作用于该地区或其驻军、长官的条款自动延至本季末结算时执行（「驿程遥远，令达于下季」）。
- 奏到日期：季首（历法月份）加驿程推算「某月某日自某地奏到」；晚到者按「原发生季首 + 一月 + 驿程」推算，落在本季之内。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.court import capital_region, rule
from simcore.domain.models import Fact, WorldState
from simcore.domain.rules import find_route

DEFAULT_LATE_THRESHOLD = 60
DERIVED_DAYS = (3, 15, 40, 70)  # 行程 0 / 1 / 2 / ≥3 季
UNIT_SEASONS = {"month": 1 / 3, "season": 1.0, "year": 4.0}
UNIT_MONTHS = {"month": 1, "season": 3, "year": 12}
DISPATCH_PREFIX = "_internal:courier:memorial:"
LATE_REPORTS_KEY = "_internal:courier_reports"
COURIER_DELAY_TEXT = "驿程遥远，令达于下季"


def late_threshold(state: WorldState, settings: dict[str, Any] | None = None) -> int:
    config = rule(state, settings, "courier")
    value = config.get("late_threshold_days") if isinstance(config, dict) else None
    try:
        return int(value) if value is not None else DEFAULT_LATE_THRESHOLD
    except (TypeError, ValueError):
        return DEFAULT_LATE_THRESHOLD


def derived_days(seasons: float | None) -> int:
    if seasons is None:
        return DERIVED_DAYS[-1]
    index = min(len(DERIVED_DAYS) - 1, max(0, int(round(seasons))))
    return DERIVED_DAYS[index]


def courier_days(state: WorldState, region_id: str | None, settings: dict[str, Any] | None = None) -> int | None:
    """某地到京城的驿程日数；地区不存在时 None。"""
    region = state.regions.get(str(region_id)) if region_id else None
    if region is None:
        return None
    if region.courier_days is not None:
        return int(region.courier_days)
    capital = capital_region(state, settings)
    if capital is None or capital == region.id:
        return DERIVED_DAYS[0]
    found = find_route(state, capital, region.id, None)
    if found is None:
        return derived_days(None)
    unit = str(state.rules.get("turn_unit", "season"))
    return derived_days(found[1] * UNIT_SEASONS.get(unit, 1.0))


def is_far(state: WorldState, region_id: str | None, settings: dict[str, Any] | None = None) -> bool:
    days = courier_days(state, region_id, settings)
    return days is not None and days > late_threshold(state, settings)


_MONTHS = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "腊")
_DIGITS = "〇一二三四五六七八九"


def _day_name(day: int) -> str:
    if day <= 10:
        return "初" + ("十" if day == 10 else _DIGITS[day])
    if day < 20:
        return "十" + _DIGITS[day - 10]
    if day == 20:
        return "二十"
    if day < 30:
        return "廿" + _DIGITS[day - 20]
    return "三十"


def date_text(start_month: int, offset_days: int) -> str:
    """季首（月份）后第 offset_days 日：「三月十八日」（每月按 30 日计）。"""
    month = (start_month - 1 + offset_days // 30) % 12
    day = offset_days % 30 + 1
    return f"{_MONTHS[month]}月{_day_name(day)}日"


def arrival_text(state: WorldState, region_id: str, settings: dict[str, Any] | None = None, late: bool = False) -> str:
    """「某月某日自某地奏到」。"""
    days = courier_days(state, region_id, settings) or 0
    region = state.regions[region_id]
    unit = str(state.rules.get("turn_unit", "season"))
    months = UNIT_MONTHS.get(unit, 3)
    start = int(state.calendar.month or 1)
    if late:
        # 原发生于上季：上季首 + 一月起事 + 驿程，换算到本季首之后的日数
        offset = max(0, 30 + days - months * 30)
    else:
        offset = days
    if region_id == state.rules.get("capital_region") and not late:
        return f"{date_text(start, offset)}奏上"  # 京中之奏，不说「自京城奏到」
    return f"{date_text(start, offset)}自{region.name}奏到"


# ---- 奏到：远地奏疏晚一季 ---------------------------------------------------------------


def memorial_dispatch_key(memorial_id: str) -> str:
    return f"{DISPATCH_PREFIX}{memorial_id}"


def memorial_is_far(item: dict[str, Any], state: WorldState) -> bool:
    region = item.get("region")
    return bool(region) and str(region) in state.regions and is_far(state, str(region))


def record_memorial_dispatches(scenario: dict[str, Any], before: WorldState, updated: WorldState) -> None:
    """结算时：远地、写了 conditions 的奏疏若在上季（before）已可见（未处理、窗口内、条件成立），记下发出之季（内部旗标）。"""
    from simcore.domain.memorials import base_visible

    for item in scenario.get("memorials") or []:
        if not isinstance(item, dict) or "conditions" not in item or not memorial_is_far(item, before):
            continue
        key = memorial_dispatch_key(str(item.get("id")))
        if key in updated.flags:
            continue
        if base_visible(item, before, before.turn):
            updated.flags[key] = before.turn


# ---- 军报：远地事件晚一季 ---------------------------------------------------------------


def event_is_far(event: dict[str, Any], state: WorldState) -> bool:
    region = event.get("region")
    return bool(region) and str(region) in state.regions and is_far(state, str(region))


def defer_event_report(state: WorldState, event_entry: dict[str, Any], facts: list[Fact], turn: int) -> None:
    """远地事件的军报（事件条目与可见事实）存入内部旗标，下季结算时送达。"""
    stored = list(state.flags.get(LATE_REPORTS_KEY) or [])
    stored.append({
        "origin_turn": turn,
        "event": dict(event_entry),
        "facts": [fact.model_dump(mode="json") for fact in facts],
    })
    state.flags[LATE_REPORTS_KEY] = stored


def release_late_reports(state: WorldState, turn: int) -> tuple[list[dict[str, Any]], list[Fact]]:
    """取出上季（及更早）存下的远地军报：返回事件条目与事实（均带 arrived_late 与原发生回合）。"""
    stored = list(state.flags.get(LATE_REPORTS_KEY) or [])
    due = [row for row in stored if int(row.get("origin_turn", turn)) < turn]
    keep = [row for row in stored if int(row.get("origin_turn", turn)) >= turn]
    if keep:
        state.flags[LATE_REPORTS_KEY] = keep
    else:
        state.flags.pop(LATE_REPORTS_KEY, None)
    events: list[dict[str, Any]] = []
    facts: list[Fact] = []
    for row in due:
        origin = int(row.get("origin_turn", turn - 1))
        event = dict(row.get("event") or {})
        region = event.get("region")
        arrival = arrival_text(state, str(region), late=True) if region in state.regions else ""
        event.update({"arrived_late": True, "origin_turn": origin, "arrival": arrival})
        events.append(event)
        for raw in row.get("facts") or []:
            fact = Fact.model_validate(raw)
            fact.arrived_late = True
            fact.origin_turn = origin
            if fact.kind == "event" and arrival:
                fact.text = f"{fact.text}（军报晚到：{arrival}）"
            elif fact.kind == "resource":
                fact.text = f"{fact.text}（已计入上季对账，军报今到）"
            facts.append(fact)
    return events, facts


# ---- 令达：作用于远地的条款延至下季 -------------------------------------------------------


def op_target_regions(state: WorldState, op: dict[str, Any]) -> list[str]:
    """条款作用的地区：军队的驻地（在途为出发地）、人物所在地、工程地区、地区指标的调整（adjust_region）。"""
    name = op.get("op")
    regions: list[str] = []
    army = state.armies.get(str(op.get("army"))) if op.get("army") else None
    if army is not None and name in {"move_army", "march", "detach", "set_army_posture", "set_army_commander", "adjust_army"}:
        regions.append(army.region)
    character = state.characters.get(str(op.get("character"))) if op.get("character") else None
    if character is not None and name in {"appoint", "dismiss", "move_character", "set_character_status"}:
        regions.append(character.region)
    if name in {"start_project", "adjust_region"} and op.get("region") in state.regions:
        regions.append(str(op.get("region")))
    return regions


def far_target(state: WorldState, ops: list[dict[str, Any]], settings: dict[str, Any] | None = None) -> tuple[str, int] | None:
    """一组条款中作用于远地者：(地区 id, 驿程日数)；没有则 None。"""
    worst: tuple[str, int] | None = None
    for op in ops:
        if not isinstance(op, dict):
            continue
        for region in op_target_regions(state, op):
            if is_far(state, region, settings):
                days = courier_days(state, region, settings) or 0
                if worst is None or days > worst[1]:
                    worst = (region, days)
    return worst


def courier_delay_reason(state: WorldState, region_id: str, days: int) -> str:
    return f"{COURIER_DELAY_TEXT}（{state.regions[region_id].name}去京城驿程约 {days} 日）"
