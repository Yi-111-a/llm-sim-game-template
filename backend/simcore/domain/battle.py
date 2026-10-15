"""最小战斗与脚本化 AI 行军。所有随机都来自调用方传入的种子 RNG。"""

from __future__ import annotations

import random
from typing import Any

from simcore.domain.events import matches, relation_value
from simcore.domain.models import Army, Fact, WorldState
from simcore.domain.rules import commander_presence

LOSER_LOSS = 0.30
WINNER_LOSS = 0.10
FORTRESS_DEFENSE = 80
FORTRESS_RATIO = 2.0


def hostile(state: WorldState, a: str | None, b: str | None, wars: list[Any] | None = None) -> bool:
    """两势力是否敌对：互相关系 < 0，或在场景 wars 列表中。"""
    if not a or not b or a == b:
        return False
    for pair in wars or []:
        members = set(pair.values()) if isinstance(pair, dict) else set(pair)
        if {a, b} <= members:
            return True
    return relation_value(state, a, b) < 0 or relation_value(state, b, a) < 0


def resolve_battles(
    state: WorldState,
    rng: random.Random,
    wars: list[Any] | None = None,
    arrived: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[Fact]]:
    """就地结算本季所有区域内的交战，返回战报与事实。

    arrived 为本季抵达的军队 id；只有新抵达的敌军才会占领无守军的地区。
    """
    battles: list[dict[str, Any]] = []
    facts: list[Fact] = []
    defense_stat = str(state.rules.get("battle_defense_stat", "defense"))
    for region_id in sorted(state.regions):
        region = state.regions[region_id]
        for _ in range(8):  # 同一地区多个敌对势力时逐对结算，设上限防止循环
            present = _present(state, region_id)
            factions = sorted({army.faction for army in present if army.faction})
            pair = _first_hostile_pair(state, factions, region.controller, wars)
            if pair is None:
                break
            defender, attacker = pair
            battle, battle_facts = _fight(state, region_id, attacker, defender, rng, defense_stat)
            battles.append(battle)
            facts.extend(battle_facts)
            if battle["result"] == "stalemate":
                break
        facts.extend(_occupy(state, region_id, wars, arrived or set()))
    return battles, facts


def _present(state: WorldState, region_id: str) -> list[Army]:
    return [
        army
        for army in state.armies.values()
        if army.region == region_id and not army.in_transit and army.strength > 0
    ]


def _first_hostile_pair(
    state: WorldState, factions: list[str], controller: str | None, wars: list[Any] | None
) -> tuple[str, str] | None:
    ordered = sorted(factions, key=lambda item: (item != controller, item))
    for index, defender in enumerate(ordered):
        for attacker in ordered[index + 1:]:
            if hostile(state, defender, attacker, wars):
                return defender, attacker
    return None


def _fight(
    state: WorldState,
    region_id: str,
    attacker: str,
    defender: str,
    rng: random.Random,
    defense_stat: str,
) -> tuple[dict[str, Any], list[Fact]]:
    region = state.regions[region_id]
    attackers = [army for army in _present(state, region_id) if army.faction == attacker]
    defenders = [army for army in _present(state, region_id) if army.faction == defender]
    defense = float(region.stats.get(defense_stat, 0)) if region.controller == defender else 0.0
    attack_power = sum(army.strength * army.quality for army in attackers) * rng.uniform(0.85, 1.15)
    defend_power = sum(army.strength * army.quality for army in defenders) * (1 + defense / 100) * rng.uniform(0.85, 1.15)
    battle: dict[str, Any] = {
        "region": region_id,
        "region_name": region.name,
        "attacker": attacker,
        "defender": defender,
        "attacker_armies": [army.id for army in attackers],
        "defender_armies": [army.id for army in defenders],
        "attacker_power": round(attack_power, 1),
        "defender_power": round(defend_power, 1),
        "defense_bonus": defense,
        "losses": {},
        "retreats": {},
        "captured": False,
    }
    attacker_name = _faction_name(state, attacker)
    defender_name = _faction_name(state, defender)
    battle["attacker_name"] = attacker_name
    battle["defender_name"] = defender_name
    battle["attacker_army_names"] = [army.name for army in attackers]
    battle["defender_army_names"] = [army.name for army in defenders]
    facts: list[Fact] = []
    # 统帅是否在军中：不在（赴任中 / 远在他处）时由副将暂摄，记入战报与事实（战力公式不变）
    battle["commanders"] = {}
    for army in [*attackers, *defenders]:
        presence = commander_presence(state, army)
        battle["commanders"][army.id] = {"name": presence["name"], "present": presence["present"], "note": presence["note"]}
        if army.commander and not presence["present"]:
            facts.append(Fact(kind="command", text=f"{army.name}：{presence['note']}", entity=army.id))
    if defense >= FORTRESS_DEFENSE and attack_power < FORTRESS_RATIO * defend_power:
        battle["result"] = "stalemate"
        battle.update({"winner": None, "winner_name": "", "attacker_loss": 0.0, "defender_loss": 0.0})
        battle["text"] = f"{attacker_name}攻{defender_name}，{defender_name}据险而守，相持一季。"
        facts.append(Fact(kind="battle", text=f"{attacker_name}攻{region.name}，{defender_name}据险而守，相持一季", entity=region_id))
        return battle, facts
    attacker_won = attack_power > defend_power
    battle["result"] = "attacker_won" if attacker_won else "defender_won"
    winners, losers = (attackers, defenders) if attacker_won else (defenders, attackers)
    loser_faction = defender if attacker_won else attacker
    for army in winners:
        loss = round(army.strength * WINNER_LOSS, 2)
        battle["losses"][army.id] = loss
        before = army.strength
        army.strength = max(0.0, army.strength - loss)
        facts.append(Fact(kind="army", text=f"{army.name}战损", entity=army.id, before=before, after=army.strength))
    for army in losers:
        loss = round(army.strength * LOSER_LOSS, 2)
        battle["losses"][army.id] = loss
        before = army.strength
        army.strength = max(0.0, army.strength - loss)
        facts.append(Fact(kind="army", text=f"{army.name}败绩", entity=army.id, before=before, after=army.strength))
        retreat = _retreat_target(state, region_id, loser_faction)
        battle["retreats"][army.id] = retreat
        if retreat is None:
            routed = army.strength
            army.strength = 0.0
            facts.append(Fact(kind="army", text=f"{army.name}无路可退，溃散", entity=army.id, before=routed, after=0.0))
        else:
            army.region = retreat
            facts.append(Fact(kind="retreat", text=f"{army.name}退往{state.regions[retreat].name}", entity=army.id, before=region_id, after=retreat))
    winner_name = attacker_name if attacker_won else defender_name
    battle["winner"] = attacker if attacker_won else defender
    battle["winner_name"] = winner_name
    battle["attacker_loss"] = round(sum(battle["losses"].get(army.id, 0.0) for army in attackers), 2)
    battle["defender_loss"] = round(sum(battle["losses"].get(army.id, 0.0) for army in defenders), 2)
    facts.insert(0, Fact(kind="battle", text=f"{region.name}之战：{attacker_name}攻，{defender_name}守，{winner_name}胜", entity=region_id))
    if attacker_won and region.controller != attacker and not any(
        army.faction == region.controller for army in _present(state, region_id)
    ):
        before = region.controller
        region.controller = attacker
        battle["captured"] = True
        facts.append(Fact(kind="region_controller", text=f"{attacker_name}攻占{region.name}", entity=region_id, before=before, after=attacker))
    battle["text"] = f"{attacker_name}攻{defender_name}，{winner_name}胜" + (f"，遂克{region.name}。" if battle["captured"] else "。")
    return battle, facts


def _retreat_target(state: WorldState, region_id: str, faction: str) -> str | None:
    region = state.regions[region_id]
    candidates = sorted(
        (turns, neighbor)
        for neighbor, turns in region.adjacency.items()
        if neighbor in state.regions and state.regions[neighbor].controller == faction
    )
    return candidates[0][1] if candidates else None


def _occupy(state: WorldState, region_id: str, wars: list[Any] | None, arrived: set[str]) -> list[Fact]:
    region = state.regions[region_id]
    present = _present(state, region_id)
    if any(army.faction == region.controller for army in present):
        return []
    newcomers = [army for army in present if army.id in arrived and hostile(state, army.faction, region.controller, wars)]
    if not newcomers or region.controller is None:
        return []
    strongest = max(newcomers, key=lambda army: (army.strength * army.quality, army.id))
    occupier = strongest.faction
    if any(hostile(state, army.faction, occupier, wars) for army in present):
        return []
    before = region.controller
    region.controller = occupier
    return [Fact(kind="region_controller", text=f"{_faction_name(state, occupier)}进占{region.name}", entity=region_id, before=before, after=occupier)]


def advance_ai_routes(
    state: WorldState, routes: list[dict[str, Any]], turn: int, wars: list[Any] | None = None
) -> tuple[list[Fact], list[str]]:
    """每季令脚本势力不在途的军队沿 route 前进一格。返回事实与警告。"""
    facts: list[Fact] = []
    warnings: list[str] = []
    for route_def in routes:
        faction = route_def.get("faction")
        route = [str(item) for item in route_def.get("route", [])]
        if not faction or not route:
            continue
        if not matches(route_def.get("conditions"), state, turn):
            continue
        allowed = set(route_def.get("armies") or [])
        for army in sorted(state.armies.values(), key=lambda item: item.id):
            if army.faction != faction or army.in_transit or army.strength <= 0:
                continue
            if allowed and army.id not in allowed:
                continue
            target = _next_step(route, army.region)
            if target is None:
                continue
            if any(hostile(state, row.faction, faction, wars) for row in _present(state, army.region)):
                continue  # 与敌军相持于此，不得越过
            current = state.regions.get(army.region)
            if current is None:
                continue
            if target not in current.adjacency:
                if army.region not in route:
                    continue
                warnings.append(f"AI 行军路线不相邻：{army.region}→{target}")
                continue
            if route_def.get("hold_if_outnumbered", True):
                own = sum(row.strength * row.quality for row in _present(state, army.region) if row.faction == faction)
                enemy = sum(
                    row.strength * row.quality
                    for row in _present(state, target)
                    if hostile(state, row.faction, faction, wars)
                )
                if enemy > own:
                    facts.append(Fact(kind="ai_hold", text=f"{army.name}见敌势强，驻足{current.name}", entity=army.id))
                    continue
            army.in_transit = {"to_region": target, "eta_turn": turn + current.adjacency[target]}
            escort = ""
            commander = state.characters.get(army.commander) if army.commander else None
            if commander is not None and commander.status == "alive" and commander.region == army.region and not commander.in_transit:
                commander.in_transit = dict(army.in_transit)  # 统帅随军同行
                escort = f"，{commander.name}随军"
            facts.append(
                Fact(kind="movement", text=f"{army.name}向{state.regions[target].name}进军{escort}", entity=army.id, before=army.region, after=target)
            )
    return facts, warnings


def _next_step(route: list[str], position: str) -> str | None:
    if position not in route:
        return route[0] if route else None
    index = route.index(position)
    return route[index + 1] if index + 1 < len(route) else None


def _faction_name(state: WorldState, faction_id: str | None) -> str:
    if faction_id is None:
        return "无主"
    faction = state.factions.get(faction_id)
    return faction.name if faction else "不明势力"
