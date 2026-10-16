"""朝会日程（agenda）与廷议在场限制（第 9 轮，CONTRACT §7.1–7.2）。

- 剧本 `settings.agenda: {audience_per_season, debate_per_season, remote_reply?}`（开局拷入 rules）；缺省不限。
  计数存 `WorldState.counters` 的 `_agenda:audience` / `_agenda:debate`（属状态、进入哈希），每季结算时清零。
  只有配置了上限的种类才计数（未配置的剧本状态与哈希不变）。
- 远地召对（书面奏对 / 遣使问对）在配置了 agenda 的剧本里不当场回话：回奏于下季送达（`remote_reply: next_season`，
  未配置 agenda 时缺省 `immediate`，保持旧行为）。
- 京城（`capital_region`）：`settings.capital_region`，缺省为玩家人物所在地。只有人在京城且不在途者能当场廷议。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.models import WorldState
from simcore.domain.rules import player_character_id, travel_note

AGENDA_PREFIX = "_agenda:"
AGENDA_KINDS = {"audience": ("audience_per_season", "召对"), "debate": ("debate_per_season", "廷议")}


def rule(state: WorldState, settings: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """规则参数：先取开局拷入的 rules，再取剧本当前 settings（旧存档开局时尚无此项）。"""
    if key in state.rules:
        return state.rules[key]
    return (settings or {}).get(key, default)


def agenda_config(state: WorldState, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    config = rule(state, settings, "agenda")
    return config if isinstance(config, dict) else {}


def agenda_limit(state: WorldState, kind: str, settings: dict[str, Any] | None = None) -> int | None:
    key = AGENDA_KINDS[kind][0]
    value = agenda_config(state, settings).get(key)
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def agenda_key(kind: str) -> str:
    return f"{AGENDA_PREFIX}{kind}"


def agenda_used(state: WorldState, kind: str) -> int:
    return int(state.counters.get(agenda_key(kind), 0) or 0)


def agenda_view(state: WorldState, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """/view.agenda：{audience: {used, limit, left}, debate: {...}, remote_reply, text}（limit 为 null 表示不限）。"""
    rows: dict[str, Any] = {}
    parts = []
    for kind, (_, label) in AGENDA_KINDS.items():
        limit = agenda_limit(state, kind, settings)
        used = agenda_used(state, kind)
        rows[kind] = {"used": used, "limit": limit, "left": None if limit is None else max(0, limit - used), "label": label}
        parts.append(f"{label} {used}/{limit}" if limit is not None else f"{label} {used}/不限")
    rows["remote_reply"] = remote_reply_mode(state, settings)
    rows["text"] = "、".join(parts)
    return rows


def agenda_refusal(state: WorldState, kind: str, settings: dict[str, Any] | None = None) -> str | None:
    """本季该种朝会已满时的中文说明；未满或不限返回 None。"""
    limit = agenda_limit(state, kind, settings)
    if limit is None:
        return None
    used = agenda_used(state, kind)
    if used < limit:
        return None
    label = AGENDA_KINDS[kind][1]
    return f"本季{label}已满（已用 {used}/{limit}，剩余 0 次）；下季结算后重置。"


def agenda_counts(state: WorldState, kind: str, settings: dict[str, Any] | None = None) -> bool:
    """该种朝会是否计数（只有配置了上限的才计数）。"""
    return agenda_limit(state, kind, settings) is not None


def remote_reply_mode(state: WorldState, settings: dict[str, Any] | None = None) -> str:
    """远地召对的回话方式：next_season（回奏于下季送达）或 immediate（当场书面作答，旧行为）。"""
    config = agenda_config(state, settings)
    mode = config.get("remote_reply")
    if mode in {"next_season", "immediate"}:
        return str(mode)
    return "next_season" if config else "immediate"


def reset_agenda(state: WorldState) -> None:
    """每季结算时清零（只删已有的计数键，未计数的剧本状态不变）。"""
    for key in [key for key in state.counters if key.startswith(AGENDA_PREFIX)]:
        del state.counters[key]


def capital_region(state: WorldState, settings: dict[str, Any] | None = None) -> str | None:
    """京城：settings.capital_region，缺省为玩家人物所在地；都没有时为 None（不设在场限制）。"""
    region = rule(state, settings, "capital_region")
    if region and str(region) in state.regions:
        return str(region)
    player = player_character_id(state, settings)
    if player and player in state.characters:
        return state.characters[player].region
    return None


def presence(state: WorldState, character_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """人物是否在京可当场廷议：{character, name, present, region, region_name, in_transit, whereabouts}。"""
    character = state.characters[character_id]
    capital = capital_region(state, settings)
    where = state.regions[character.region].name if character.region in state.regions else character.region
    in_transit = travel_note(state, character) if character.in_transit else None
    present = character.status == "alive" and not character.in_transit and (capital is None or character.region == capital)
    if in_transit:
        whereabouts = f"在途（{in_transit}）"
    else:
        whereabouts = f"在{where}"
    return {
        "character": character_id,
        "name": character.name,
        "present": present,
        "region": character.region,
        "region_name": where,
        "in_transit": in_transit,
        "whereabouts": whereabouts,
    }


def present_list(state: WorldState, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET /present：在京可廷议者与在外者（所在、行程）。玩家本人、不在世者与他国之人不列。"""
    capital = capital_region(state, settings)
    player = player_character_id(state, settings)
    own = state.rules.get("player_faction")
    present: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    for character in state.characters.values():
        if character.id == player or character.status != "alive":
            continue
        if own and character.faction and character.faction != own:
            continue
        row = presence(state, character.id, settings)
        row["offices"] = [state.offices[office].name for office in character.offices if office in state.offices]
        (present if row["present"] else absent).append(row)
    return {
        "capital": {"id": capital, "name": state.regions[capital].name if capital in state.regions else None} if capital else None,
        "present": present,
        "absent": absent,
    }


def not_present_message(state: WorldState, character_id: str, settings: dict[str, Any] | None = None) -> str:
    row = presence(state, character_id, settings)
    capital = capital_region(state, settings)
    capital_name = state.regions[capital].name if capital in state.regions else "京城"
    return f"{row['name']}不在{capital_name}（{row['whereabouts']}），不能当场廷议；可列入 written 递书面议状，其议于下季送达"
