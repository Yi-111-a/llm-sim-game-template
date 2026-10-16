"""军令与将领自主（第 9 轮，CONTRACT §7.5）。

军令姿态（`set_army_posture`：hold 坚守 / attack 进击 / scorch 坚壁清野；可由 `settings.postures` 覆盖系数）：
- hold：作守方时战力 +20%；守方得胜不追击（败方只折二成）；作攻方时按兵不动，不主动交战。
- attack：作攻方时战力 +10%；败则多损（折四成）。
- scorch：驻地（本方控制）每季指标下降（缺省 繁荣 −5、粮产 −3，只作用于地区已有的指标）；
  在此地的敌军补给受损：攻战力 −15%，每季折损 5%。

将领自主（`settings.autonomy: {formula, by_order?, outcomes?, levels?}`）：对有统帅之军下的军令（march / move_army / detach /
set_army_posture），按公式算出「拒命或擅动」概率（统帅的特质为变量，另有 is_march / is_detach / is_hold / is_attack /
is_scorch / is_move、present、strength（万））。拟诏回执只给风险等级（低 / 中 / 高）；确认时以种子随机判定，命中则该条款
延至下季结算时执行（delay：迁延一季照行；alter：改作他令；sortie：擅自出战），结算时写成事实（kind: autonomy）。
"""

from __future__ import annotations

import ast
import hashlib
import random
from typing import Any

from simcore.domain.battle import hostile
from simcore.domain.formulas import evaluate
from simcore.domain.models import Army, Fact, WorldState
from simcore.domain.rules import clamp_stat, commander_presence, find_route, mover_faction, stat_name, travel_note

POSTURES = {"hold": "坚守", "attack": "进击", "scorch": "坚壁清野"}
DEFAULT_EFFECTS: dict[str, dict[str, Any]] = {
    "hold": {"defense_bonus": 0.2, "loser_loss": 0.2},
    "attack": {"attack_bonus": 0.1, "loser_loss": 0.4},
    "scorch": {"stats": {"prosperity": -5, "grain": -3}, "enemy_supply": 0.15, "enemy_attrition": 0.05},
}
MILITARY_ORDERS = frozenset({"march", "move_army", "detach", "set_army_posture"})
AUTONOMY_OUTCOMES = {"delay": 0.5, "alter": 0.3, "sortie": 0.2}
RISK_LEVELS = (0.1, 0.3)  # < 0.1 低；< 0.3 中；其余高


def posture_label(posture: str | None) -> str:
    return POSTURES.get(str(posture), "常态") if posture else "常态"


def posture_effects(state: WorldState) -> dict[str, dict[str, Any]]:
    effects = {key: dict(value) for key, value in DEFAULT_EFFECTS.items()}
    override = state.rules.get("postures")
    if isinstance(override, dict):
        for key, value in override.items():
            if key in effects and isinstance(value, dict):
                effects[key].update(value)
    return effects


def _wars(state: WorldState, wars: list[Any] | None) -> list[Any]:
    if wars is not None:
        return wars
    raw = state.rules.get("wars")
    return raw if isinstance(raw, list) else []


def power_modifier(state: WorldState, army: Army, role: str, region_id: str, wars: list[Any] | None = None) -> tuple[float, list[str]]:
    """某军在此战中的军令系数与说明：role 为 attacker / defender。"""
    effects = posture_effects(state)
    factor = 1.0
    notes: list[str] = []
    if army.posture == "hold" and role == "defender":
        bonus = float(effects["hold"].get("defense_bonus", 0) or 0)
        factor *= 1 + bonus
        notes.append(f"坚守+{bonus * 100:g}%")
    if army.posture == "attack" and role == "attacker":
        bonus = float(effects["attack"].get("attack_bonus", 0) or 0)
        factor *= 1 + bonus
        notes.append(f"进击+{bonus * 100:g}%")
    if role == "attacker" and scorched_against(state, army, region_id, wars):
        supply = float(effects["scorch"].get("enemy_supply", 0) or 0)
        factor *= 1 - supply
        notes.append(f"坚壁清野，补给受损−{supply * 100:g}%")
    return factor, notes


def scorched_against(state: WorldState, army: Army, region_id: str, wars: list[Any] | None = None) -> bool:
    """此地有与该军敌对、取坚壁清野之势的军队（不在途）。"""
    return any(
        row.posture == "scorch" and row.region == region_id and not row.in_transit and row.strength > 0
        and hostile(state, row.faction, army.faction, _wars(state, wars))
        for row in state.armies.values()
    )


def loser_loss(state: WorldState, base: float, loser: Army, winners: list[Army], winner_role: str) -> float:
    """败方折损比例：守方以坚守之势得胜不追击（折损降至 hold.loser_loss）；败方取进击之势则多损。"""
    effects = posture_effects(state)
    loss = base
    if winner_role == "defender" and winners and all(row.posture == "hold" for row in winners):
        loss = min(loss, float(effects["hold"].get("loser_loss", base) or base))
    if loser.posture == "attack":
        loss = max(loss, float(effects["attack"].get("loser_loss", base) or base))
    return loss


def all_holding(armies: list[Army]) -> bool:
    return bool(armies) and all(row.posture == "hold" for row in armies)


def scorch_effects(state: WorldState, wars: list[Any] | None = None) -> list[Fact]:
    """每季：取坚壁清野之势、驻本方控制地区的军队，令当地指标下降（同一地区每季只计一次）。"""
    effects = posture_effects(state)["scorch"].get("stats") or {}
    facts: list[Fact] = []
    done: set[str] = set()
    for army in sorted(state.armies.values(), key=lambda row: row.id):
        if army.posture != "scorch" or army.in_transit or army.strength <= 0 or army.region in done:
            continue
        region = state.regions.get(army.region)
        if region is None or region.controller not in (None, army.faction):
            continue
        done.add(region.id)
        parts = []
        for stat, delta in effects.items():
            if stat not in region.stats or not isinstance(delta, (int, float)):
                continue
            before = region.stats[stat]
            after = clamp_stat(state, stat, before + float(delta))
            region.stats[stat] = after
            parts.append(f"{stat_name(state, stat)} {after - before:+g}")
        if parts:
            facts.append(Fact(kind="region", text=f"{army.name}坚壁清野：{region.name}{'、'.join(parts)}", entity=region.id, cause="坚壁清野"))
    return facts


def posture_attrition(state: WorldState, wars: list[Any] | None = None) -> list[Fact]:
    """坚壁清野之地的敌军补给受损：每季折损 enemy_attrition。"""
    rate = float(posture_effects(state)["scorch"].get("enemy_attrition", 0) or 0)
    if rate <= 0:
        return []
    facts: list[Fact] = []
    for army in sorted(state.armies.values(), key=lambda row: row.id):
        if army.in_transit or army.strength <= 0 or not scorched_against(state, army, army.region, wars):
            continue
        before = army.strength
        army.strength = max(0.0, round(before * (1 - rate), 2))
        region = state.regions[army.region].name if army.region in state.regions else army.region
        facts.append(Fact(kind="army", text=f"{army.name}在{region}因坚壁清野乏粮减员", entity=army.id, before=before, after=army.strength))
    return facts


# ---- 将领自主 -------------------------------------------------------------------------


def autonomy_config(state: WorldState) -> dict[str, Any]:
    config = state.rules.get("autonomy")
    return config if isinstance(config, dict) else {}


def order_army(state: WorldState, op: dict[str, Any]) -> Army | None:
    if op.get("op") not in MILITARY_ORDERS:
        return None
    return state.armies.get(str(op.get("army")))


def order_kind(op: dict[str, Any]) -> str:
    name = str(op.get("op"))
    if name == "set_army_posture":
        return str(op.get("posture"))
    return {"move_army": "move"}.get(name, name)


def autonomy_probability(state: WorldState, op: dict[str, Any]) -> float | None:
    """有统帅之军的军令「拒命或擅动」概率（0–1）；无配置、非军令、无统帅时 None。内部值，不对外显示。"""
    config = autonomy_config(state)
    army = order_army(state, op)
    if not config or army is None or not army.commander or army.commander not in state.characters:
        return None
    kind = order_kind(op)
    by_order = config.get("by_order") if isinstance(config.get("by_order"), dict) else {}
    formula = by_order.get(kind, config.get("formula"))
    if formula is None:
        return None
    commander = state.characters[army.commander]
    # 公式里出现而此人没有的特质按 0（如 rashness 轻躁）
    values: dict[str, Any] = {name: 0.0 for name in formula_names(str(formula))}
    values.update({key: 0.0 for row in state.characters.values() for key in row.traits})
    values.update({key: float(value) for key, value in commander.traits.items() if isinstance(value, (int, float))})
    for name in ("march", "detach", "hold", "attack", "scorch", "move"):
        values[f"is_{name}"] = 1.0 if kind == name else 0.0
    values["present"] = 1.0 if commander_presence(state, army)["present"] else 0.0
    values["strength"] = army.strength / 10000
    try:
        probability = evaluate(formula, values)
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return min(1.0, max(0.0, probability))


def formula_names(formula: str) -> set[str]:
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError:
        return set()
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - calls


def risk_level(state: WorldState, probability: float | None) -> str | None:
    if probability is None:
        return None
    levels = autonomy_config(state).get("levels")
    low, mid = RISK_LEVELS
    if isinstance(levels, list) and len(levels) == 2:
        low, mid = float(levels[0]), float(levels[1])
    return "低" if probability < low else "中" if probability < mid else "高"


def autonomy_preview(state: WorldState, op: dict[str, Any]) -> dict[str, Any] | None:
    """拟诏回执的风险预览：{level, commander, text}（不含概率真值）。"""
    probability = autonomy_probability(state, op)
    if probability is None:
        return None
    army = order_army(state, op)
    assert army is not None and army.commander
    name = state.characters[army.commander].name
    level = risk_level(state, probability)
    return {"level": level, "commander": name, "text": f"将领自主风险：{level}（{army.name}主将{name}）"}


def roll_autonomy(state: WorldState, op: dict[str, Any], seed: int) -> dict[str, Any] | None:
    """确认时判定：命中返回 {result: delay|alter|sortie, ops, label}；未命中或无风险返回 None。种子随机，同局同季同令稳定。"""
    probability = autonomy_probability(state, op)
    if not probability:
        return None
    key = f"autonomy:{seed}:{state.turn}:{op.get('op')}:{op.get('army')}:{op.get('to_region')}:{op.get('posture')}:{op.get('strength')}"
    rng = random.Random(int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16))
    if rng.random() >= probability:
        return None
    weights = autonomy_config(state).get("outcomes")
    table = {key: float(value) for key, value in (weights.items() if isinstance(weights, dict) else AUTONOMY_OUTCOMES.items())
             if key in AUTONOMY_OUTCOMES and isinstance(value, (int, float)) and value > 0} or dict(AUTONOMY_OUTCOMES)
    pick = rng.random() * sum(table.values())
    result = next(iter(table))
    for name, weight in table.items():
        pick -= weight
        if pick <= 0:
            result = name
            break
    army = order_army(state, op)
    assert army is not None and army.commander
    commander = state.characters[army.commander].name
    if result == "sortie":
        target = sortie_target(state, army)
        if target is not None:
            ops = [{"op": "set_army_posture", "army": army.id, "posture": "attack"}, {"op": "march", "army": army.id, "to_region": target}]
            label = f"将领自主：{commander}不待诏命，率{army.name}擅自出战，进击{state.regions[target].name}"
            return {"result": "sortie", "ops": ops, "label": label}
        result = "alter"
    if result == "alter":
        if op.get("op") == "set_army_posture" and op.get("posture") == "hold":
            ops = [{"op": "set_army_posture", "army": army.id, "posture": "attack"}]
            label = f"将领自主：{commander}不肯坐守，{army.name}改取进击之势"
        else:
            ops = [{"op": "set_army_posture", "army": army.id, "posture": "hold"}]
            label = f"将领自主：{commander}以为不可轻动，{army.name}未奉前令，改取坚守之势"
        return {"result": "alter", "ops": ops, "label": label}
    return {"result": "delay", "ops": [op], "label": f"将领自主：{commander}迁延不进，{army.name}之令迟一季方行"}


def sortie_target(state: WorldState, army: Army) -> str | None:
    """擅自出战的目标：相邻地区中有敌军或为敌方所控者（行程最短、id 序最小）。"""
    region = state.regions.get(army.region)
    if region is None:
        return None
    wars = _wars(state, None)
    candidates = []
    for neighbor, turns in sorted(region.adjacency.items()):
        target = state.regions.get(neighbor)
        if target is None:
            continue
        enemy_here = any(
            row.region == neighbor and not row.in_transit and row.strength > 0 and hostile(state, row.faction, army.faction, wars)
            for row in state.armies.values()
        )
        if enemy_here or (target.controller and hostile(state, target.controller, army.faction, wars)):
            candidates.append((turns, neighbor))
    return min(candidates)[1] if candidates else None


# ---- 兵部总览（GET /armies） -------------------------------------------------------------


def armies_overview(state: WorldState, cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """本方各军：兵力、素质、统帅（是否在营）、驻地或行程、姿态、可下的命令、到各地区的行程季数（无路为 null）。"""
    own = mover_faction(state, None)
    rows = []
    military_cards = [row for row in cards or [] if row.get("category") == "军务" and row.get("available")]
    for army in state.armies.values():
        if own is not None and army.faction != own:
            continue
        if army.strength <= 0:
            continue
        presence = commander_presence(state, army)
        region = state.regions.get(army.region)
        transit = travel_note(state, army) if army.in_transit else None
        etas: dict[str, int | None] = {}
        for region_id in state.regions:
            if army.in_transit:
                etas[region_id] = None
                continue
            found = find_route(state, army.region, region_id, army.faction)
            etas[region_id] = found[1] if found is not None else None
        orders = [] if army.in_transit else ["march", "detach", "set_army_posture", "set_army_commander"]
        if army.in_transit:
            orders = ["set_army_posture", "set_army_commander"]
        risk = None
        if not army.in_transit and army.commander:
            risk = risk_level(state, autonomy_probability(state, {"op": "march", "army": army.id, "to_region": army.region}))
        rows.append({
            "id": army.id,
            "name": army.name,
            "strength": army.strength,
            "quality": army.quality,
            "commander": {
                "id": army.commander,
                "name": presence["name"],
                "present": presence["present"],
                "note": presence["note"],
            } if army.commander else None,
            "region": army.region,
            "region_name": region.name if region else army.region,
            "in_transit": transit,
            "destination": (army.in_transit or {}).get("to_region"),
            "eta_turn": (army.in_transit or {}).get("eta_turn"),
            "whereabouts": f"在途：{transit}" if transit else f"驻{region.name if region else army.region}",
            "posture": army.posture,
            "posture_label": posture_label(army.posture),
            "orders": orders,
            "cards": [row["id"] for row in military_cards],
            "autonomy_risk": risk,
            "etas": etas,
        })
    return rows
