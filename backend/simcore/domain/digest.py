"""StateDigest：引擎渲染的事实摘要（固定格式中文文本），供 LLM 提示词与 MCP 使用。

只使用公开视图可见的数值：隐藏资源一律给区间。传入 character_id 时只给该人物能知道的部分：
本人身份、所在及相邻地区、本人统领或驻在附近的军队；全国资源与官职在任者人人可知。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.models import TurnReport, WorldState
from simcore.domain.rules import held_offices
from simcore.domain.visibility import fact_line, public_report, resource_display

STATUS_NAMES = {"alive": "在世", "dead": "已故", "imprisoned": "系狱", "exiled": "流放", "retired": "致仕"}


def render_digest(
    state: WorldState,
    seed: int,
    character_id: str | None = None,
    report: TurnReport | dict[str, Any] | None = None,
    scenario: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    definition = (scenario or {}).get("scenario", {}) or {}
    stat_names = {str(row.get("id")): str(row.get("name", row.get("id"))) for row in definition.get("stats", []) or [] if isinstance(row, dict)}
    character = state.characters.get(character_id) if character_id else None
    lines.append(f"【时】{state.calendar.label}（第{state.turn}回合）")
    if character is not None:
        offices = "、".join(_office_name(state, office) for office in held_offices(character)) or "无官职"
        region = state.regions.get(character.region)
        lines.append(
            f"【本人】{character.name}{('（' + character.title + '）') if character.title else ''}；{offices}；"
            f"所在 {region.name if region else character.region}；{STATUS_NAMES.get(character.status, character.status)}"
        )
    lines.append("【国势】" + "；".join(resource_display(state, key, seed) for key in state.resources))
    office_rows = []
    for office in state.offices.values():
        holder = state.characters.get(office.holder) if office.holder else None
        office_rows.append(f"{office.name}：{holder.name if holder else '出缺'}")
    if office_rows:
        lines.append("【官职】" + "；".join(office_rows))
    visible_regions = _visible_regions(state, character)
    region_rows = []
    for region_id in visible_regions:
        region = state.regions[region_id]
        stats = " ".join(f"{stat_names.get(key, key)}{_number(value)}" for key, value in region.stats.items())
        controller = state.factions.get(region.controller) if region.controller else None
        suffix = f"（{controller.name}）" if controller else ""
        region_rows.append(f"{region.name}{suffix} {stats}".strip())
    if region_rows:
        lines.append("【地区】" + "；".join(region_rows))
    army_rows = []
    for army in state.armies.values():
        if character is not None and army.commander != character.id and army.region not in visible_regions:
            continue
        faction = state.factions.get(army.faction) if army.faction else None
        commander = state.characters.get(army.commander) if army.commander else None
        region = state.regions.get(army.region)
        parts = [f"{army.name} {_number(army.strength)}", f"驻{region.name if region else army.region}"]
        if faction:
            parts.append(f"属{faction.name}")
        if commander:
            parts.append(f"统帅{commander.name}")
        if army.in_transit:
            target = state.regions.get(army.in_transit.get("to_region", ""))
            parts.append(f"行军往{target.name if target else army.in_transit.get('to_region')}")
        army_rows.append("，".join(parts))
    if army_rows:
        lines.append("【军队】" + "；".join(army_rows))
    if state.projects:
        lines.append("【工程】" + "；".join(f"{row.name} {row.progress}/{row.turns}{'（停工）' if row.stalled else ''}" for row in state.projects))
    if state.standing_orders:
        lines.append("【常设令】" + "；".join(row.text for row in state.standing_orders))
    if state.pending_choices and character is None:
        lines.append("【待决】" + "；".join(f"{row.title}（选项：{'/'.join(option.text for option in row.options)}）" for row in state.pending_choices))
    if report is not None:
        public = public_report(report, state, seed, definition.get("crisis")) or {}
        facts = [fact_line(fact) for fact in public.get("facts", []) if fact.get("kind") not in {"flag", "counter"}]
        if facts:
            lines.append("【本季事实】" + "；".join(facts[:30]))
    if state.outcome is not None:
        lines.append(f"【终局】{state.outcome.title}")
    return "\n".join(lines)


def digest_numbers(digest: str) -> set[float]:
    """摘要中出现的全部数字，供台词数字核对。"""
    import re

    return {float(item) for item in re.findall(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?", digest)}


def _visible_regions(state: WorldState, character: Any) -> list[str]:
    if character is None:
        return list(state.regions)
    home = state.regions.get(character.region)
    if home is None:
        return []
    return [home.id, *[neighbor for neighbor in home.adjacency if neighbor in state.regions]]


def _office_name(state: WorldState, office_id: str) -> str:
    office = state.offices.get(office_id)
    return office.name if office else office_id


def _number(value: float) -> str:
    rounded = round(float(value), 1)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"
