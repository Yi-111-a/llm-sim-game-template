"""StateDigest：引擎渲染的事实摘要（固定格式中文文本），供 LLM 提示词与 MCP 使用。

只使用公开视图可见的数值：隐藏资源一律给区间。传入 character_id 时只给该人物能知道的部分：
本人身份、相关地区（所在、统兵所在、所知、行程目的地、相邻）、本人统领或驻在相关地区的军队、本人记忆；
其余地区压成一行名单。全国资源与官职在任者人人可知。
非玩家势力控制的地区（外域）不列经济指标，只给控制者与驻军。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.models import TurnReport, WorldState
from simcore.domain.rules import (
    STATUS_NAMES,
    army_station,
    commander_presence,
    current_title,
    held_offices,
    stat_name,
    transit_label,
    travel_note,
    traveller_label,
)
from simcore.domain.visibility import fact_line, format_number, public_report, resource_display

__all__ = ["STATUS_NAMES", "digest_numbers", "render_digest"]


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
    player_faction = state.rules.get("player_faction")
    character = state.characters.get(character_id) if character_id else None
    lines.append(f"【时】{state.calendar.label}（第{state.turn}回合）")
    if character is not None:
        offices = "、".join(_office_name(state, office) for office in held_offices(character)) or "无官职"
        region = state.regions.get(character.region)
        where = region.name if region else character.region
        if character.in_transit:
            where += f"（{travel_note(state, character)}）"
        lines.append(
            f"【本人】{character.name}{_title_note(state, character.id, scenario)}；{offices}；"
            f"所在 {where}；{STATUS_NAMES.get(character.status, character.status)}"
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
    foreign_rows = []
    for region_id in visible_regions:
        region = state.regions[region_id]
        controller = state.factions.get(region.controller) if region.controller else None
        if not _own(region.controller, player_faction):
            garrison = [army.name for army in state.armies.values() if army.region == region_id and army.strength > 0]
            holder = controller.name if controller else str(region.controller)
            foreign_rows.append(f"{region.name}（{holder}）" + (f"驻{'、'.join(garrison)}" if garrison else ""))
            continue
        stats = " ".join(f"{stat_names.get(key) or stat_name(state, key)}{_number(value)}" for key, value in region.stats.items())
        suffix = f"（{controller.name}）" if controller and player_faction is None else ""
        region_rows.append(f"{region.name}{suffix} {stats}".strip())
    if region_rows:
        lines.append("【地区】" + "；".join(region_rows))
    if foreign_rows:
        lines.append("【外域】" + "；".join(foreign_rows))
    others = [region_id for region_id in state.regions if region_id not in visible_regions]
    if character is not None and others:
        groups: dict[str, list[str]] = {}
        for region_id in others:
            region = state.regions[region_id]
            faction = state.factions.get(region.controller) if region.controller else None
            label = "本方" if _own(region.controller, player_faction) else (faction.name if faction else str(region.controller))
            groups.setdefault(label, []).append(region.name)
        lines.append("【他处】" + "；".join(f"{label}：{'、'.join(names)}" for label, names in groups.items()))
    army_rows = []
    for army in state.armies.values():
        if army.strength <= 0 and not army.in_transit:
            continue  # 兵额为零的军号（未成军的预备番号、已溃之军）不列，免得读成某地驻有某军
        if character is not None and army.commander != character.id and army.region not in visible_regions:
            continue
        faction = state.factions.get(army.faction) if army.faction else None
        commander = state.characters.get(army.commander) if army.commander else None
        region = state.regions.get(army.region)
        parts = [f"{army.name} {_number(army.strength)}", transit_label(state, army) or f"驻{region.name if region else army.region}"]
        if faction:
            parts.append(f"属{faction.name}")
        if commander:
            presence = commander_presence(state, army)
            if presence["present"]:
                parts.append(f"统帅{commander.name}")
            elif commander.in_transit and commander.in_transit.get("to_region") == army_station(army):
                parts.append(f"统帅{commander.name}未到任（{travel_note(state, commander)}），副将暂摄")
            elif commander.in_transit:
                parts.append(f"统帅{commander.name}离军他往（{travel_note(state, commander)}），副将暂摄")
            else:
                parts.append(f"统帅{commander.name}不在军中，副将暂摄")
        army_rows.append("，".join(parts))
    if army_rows:
        lines.append("【军队】" + "；".join(army_rows))
    travellers = [traveller_label(state, row) for row in state.characters.values() if row.in_transit and row.status == "alive"]
    if travellers:
        lines.append("【在途】" + "；".join(travellers))
    if state.projects:
        lines.append("【工程】" + "；".join(f"{row.name} {row.progress}/{row.turns}{'（停工）' if row.stalled else ''}" for row in state.projects))
    if state.standing_orders:
        lines.append("【常设令】" + "；".join(row.text for row in state.standing_orders))
    if character is not None and character.memories:
        lines.append("【记忆】" + "；".join(character.memories[-12:]))
    if state.pending_choices and character is None:
        lines.append("【待决】" + "；".join(f"{row.title}（选项：{'/'.join(option.text for option in row.options)}）" for row in state.pending_choices))
    if report is not None:
        public = public_report(report, state, seed, definition.get("crisis")) or {}
        facts = [fact_line(fact) for fact in public.get("facts", [])]
        if facts:
            lines.append("【本季事实】" + "；".join(facts[:30]))
    if state.outcome is not None:
        lines.append(f"【终局】{state.outcome.title}")
    return "\n".join(lines)


def digest_numbers(digest: str) -> set[float]:
    """摘要中出现的全部数字，供台词数字核对。"""
    import re

    return {float(item) for item in re.findall(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?", digest)}


def _own(controller: str | None, player_faction: Any) -> bool:
    return player_faction is None or controller in (None, player_faction)


def _visible_regions(state: WorldState, character: Any) -> list[str]:
    """人物的相关地区，按相关程度排序：所在 → 统兵所在 → 所知（knowledge 写了地区 id 或名称）→ 行程目的地 → 相邻。"""
    if character is None:
        return list(state.regions)
    ordered: list[str] = []

    def add(region_id: Any) -> None:
        if region_id in state.regions and region_id not in ordered:
            ordered.append(str(region_id))

    add(character.region)
    for army in state.armies.values():
        if army.commander == character.id:
            add(army.region)
    for item in character.knowledge:
        for region in state.regions.values():
            if item == region.id or region.name in str(item):
                add(region.id)
    if character.in_transit:
        add(character.in_transit.get("to_region"))
    home = state.regions.get(character.region)
    for neighbor in home.adjacency if home else {}:
        add(neighbor)
    return ordered


def _office_name(state: WorldState, office_id: str) -> str:
    office = state.offices.get(office_id)
    return office.name if office else office_id


_number = format_number


def _title_note(state: WorldState, character_id: str, scenario: dict[str, Any] | None) -> str:
    """【本人】名后的头衔：官职已变时注旧衔（「（旧衔：安西节度判官）」），否则照旧写静态头衔。"""
    title, former = current_title(state, character_id, scenario)
    if former:
        return f"（旧衔：{former}）"
    return f"（{title}）" if title else ""
