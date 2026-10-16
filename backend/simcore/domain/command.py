"""官职—所在—所统—指挥状态总表（玩家势力）：谁任何职、人在何处、统哪支军队、是否在军（在军 / 赴任中 / 遥领 / 副将暂摄）。

/view 的 command_table 与 MCP briefing 的「官职与兵权」段共用；出缺与遥领列为提醒。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.models import WorldState
from simcore.domain.rules import (
    army_station,
    army_whereabouts,
    commander_presence,
    held_offices,
    mover_faction,
    office_army,
    office_station,
    transit_label,
    travel_note,
)
from simcore.domain.visibility import format_number

STATUS_LABELS = {"present": "在军", "en_route": "赴任中", "remote": "遥领", "acting": "副将暂摄"}


def _own(state: WorldState, faction: str | None) -> bool:
    own = mover_faction(state, None)
    return own is None or faction in (None, own)


def _region_name(state: WorldState, region_id: str | None) -> str:
    region = state.regions.get(str(region_id)) if region_id else None
    return region.name if region else str(region_id or "")


def _location(state: WorldState, character: Any) -> dict[str, Any]:
    note = travel_note(state, character)
    return {
        "region": character.region,
        "name": _region_name(state, character.region),
        "in_transit": note or None,
        "display": note or _region_name(state, character.region),
    }


def _command_row(state: WorldState, army: Any) -> dict[str, Any]:
    presence = commander_presence(state, army)
    if presence["present"]:
        status = "present"
    elif army.commander and state.characters[army.commander].in_transit and (
        state.characters[army.commander].in_transit or {}
    ).get("to_region") == army_station(army):
        status = "en_route"
    else:
        status = "remote"
    label = STATUS_LABELS[status]
    if status == "en_route":
        label += f"（{travel_note(state, state.characters[army.commander])}；副将暂摄）"
    elif status == "remote":
        label += "（副将暂摄）"
    return {
        "army": army.id,
        "name": army.name,
        "strength": army.strength,
        "station": army_station(army),
        "station_name": _region_name(state, army_station(army)),
        "region": army.region,
        "region_name": _region_name(state, army.region),
        "in_transit": transit_label(state, army) or None,
        "whereabouts": army_whereabouts(state, army),
        "status": status,
        "label": label,
    }


def command_table(state: WorldState, player: str | None = None) -> dict[str, Any]:
    """玩家势力的官职与兵权总表。

    返回 {rows, vacancies: {offices, armies}, alerts, columns}：
    - rows：每位本方在世人物（任官或统兵者，玩家本人除外）一行：{character, name, offices: [{id, name, station?, status?}],
      location: {region, name, in_transit, display}, commands: [{army, name, strength, station, station_name, status, label}]}；
      status 为 present（在军）/ en_route（赴任中）/ remote（遥领，副将暂摄）。
    - vacancies.offices：出缺的官职；vacancies.armies：本方无统帅的军队（副将暂摄）。
    - alerts：遥领、兼统数军、出缺、无帅的醒目提示（中文）。
    """
    rows: list[dict[str, Any]] = []
    alerts: list[str] = []
    player_offices = set(held_offices(state.characters[player])) if player in state.characters else set()
    for character in state.characters.values():
        if character.status != "alive" or character.id == player or not _own(state, character.faction):
            continue
        offices = [office for office in held_offices(character) if office in state.offices]
        armies = [row for row in state.armies.values() if row.commander == character.id and row.strength > 0]
        if not offices and not armies:
            continue
        location = _location(state, character)
        commands = [_command_row(state, army) for army in armies]
        office_rows: list[dict[str, Any]] = []
        for office_id in offices:
            entry: dict[str, Any] = {"id": office_id, "name": state.offices[office_id].name}
            station = office_station(state, office_id)
            if station is not None:
                entry["station"] = station
                entry["station_name"] = _region_name(state, station)
                transit = character.in_transit or {}
                if character.region == station and not character.in_transit:
                    entry["status"] = "present"
                elif transit.get("to_region") == station:
                    entry["status"] = "en_route"
                else:
                    entry["status"] = "remote"
                    if not office_army(state, office_id):
                        alerts.append(f"{character.name}领{entry['name']}而人在{location['display']}（遥领，未在任所{entry['station_name']}）")
            office_rows.append(entry)
        for command in commands:
            if command["status"] == "remote":
                alerts.append(f"{character.name}遥领{command['name']}（军{command['whereabouts']}，人在{location['display']}），副将暂摄")
        if len(commands) > 1:
            alerts.append(f"{character.name}兼统{'、'.join(row['name'] for row in commands)}")
        rows.append({
            "character": character.id,
            "name": character.name,
            "offices": office_rows,
            "location": location,
            "commands": commands,
        })
    vacant_offices: list[dict[str, Any]] = [
        {"id": office.id, "name": office.name, **({"army": office_army(state, office.id)} if office_army(state, office.id) else {})}
        for office in state.offices.values()
        if office.holder is None and office.id not in player_offices
    ]
    vacant_armies = [
        {
            "army": army.id,
            "name": army.name,
            "strength": army.strength,
            "station": army_station(army),
            "station_name": _region_name(state, army_station(army)),
            "region": army.region,
            "region_name": _region_name(state, army.region),
            "in_transit": transit_label(state, army) or None,
            "whereabouts": army_whereabouts(state, army),
            "status": "acting",
            "label": "无统帅（副将暂摄）",
        }
        for army in state.armies.values()
        if _own(state, army.faction) and army.strength > 0 and (army.commander is None or army.commander not in state.characters
                                                                 or state.characters[army.commander].status != "alive")
    ]
    if vacant_offices:
        alerts.append("出缺：" + "、".join(row["name"] for row in vacant_offices))
    if vacant_armies:
        alerts.append("无统帅：" + "、".join(f"{row['name']}（{row['whereabouts']}）" for row in vacant_armies))
    return {
        "columns": ["官职", "人物", "所在", "所统", "指挥状态"],
        "rows": rows,
        "vacancies": {"offices": vacant_offices, "armies": vacant_armies},
        "alerts": alerts,
    }


def command_table_lines(table: dict[str, Any]) -> list[str]:
    """简报用的 Markdown 表：「官职 | 人物 | 所在 | 所统 | 指挥状态」，遥领、出缺、无统帅加粗。"""
    if not table.get("rows") and not table["vacancies"]["offices"] and not table["vacancies"]["armies"]:
        return []
    lines = ["| " + " | ".join(table["columns"]) + " |", "|" + "---|" * len(table["columns"])]
    for row in table["rows"]:
        offices = "、".join(
            office["name"] + ("（**遥领**）" if office.get("status") == "remote" else "（赴任中）" if office.get("status") == "en_route" else "")
            for office in row["offices"]
        ) or "—"
        commands = "、".join(
            f"{command['name']}{format_number(command['strength'])}" + (f"（{command['in_transit']}）" if command.get("in_transit") else "")
            for command in row["commands"]
        ) or "—"
        status = "；".join(
            f"{command['name']} " + (f"**{command['label']}**" if command["status"] == "remote" else command["label"]) for command in row["commands"]
        ) or "—"
        lines.append(f"| {offices} | {row['name']} | {row['location']['display']} | {commands} | {status} |")
    for office in table["vacancies"]["offices"]:
        lines.append(f"| {office['name']} | **出缺** | — | — | — |")
    for army in table["vacancies"]["armies"]:
        lines.append(f"| — | **无统帅** | — | {army['name']}{format_number(army['strength'])}（{army['whereabouts']}） | 副将暂摄 |")
    return lines
