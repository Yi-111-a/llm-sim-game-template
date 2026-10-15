"""难度档（scenario.yaml `difficulties` / `default_difficulty`）。

每档：{id, label, description, drift_scale: {resource_id: factor}, gravity_scale: {event_id 或 *: factor},
start: {resource_id: value}, delay_scale: factor}。开局时写入 WorldState.rules["difficulty"]，
此后回合结算（漂移、事件概率）与拟诏（延期概率）都从 state.rules 读取，存档快照与重放都不依赖剧本当前内容。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.models import WorldState


class DifficultyError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "UNKNOWN_DIFFICULTY"
        self.message = message


def difficulty_defs(definition: dict[str, Any]) -> list[dict[str, Any]]:
    rows = definition.get("difficulties") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def public_difficulties(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """供场景列表 / 新开局界面：[{id, label, description, default}]。"""
    default = definition.get("default_difficulty")
    return [
        {
            "id": str(row.get("id")),
            "label": str(row.get("label", row.get("id"))),
            "description": str(row.get("description", "")),
            "default": row.get("id") == default,
        }
        for row in difficulty_defs(definition)
    ]


def resolve_difficulty(definition: dict[str, Any], requested: str | None) -> dict[str, Any] | None:
    """按 id 取难度档；未指定时用 default_difficulty（再缺省为第一档）。场景未定义难度时：未指定返回 None，指定则报错。"""
    rows = difficulty_defs(definition)
    if not rows:
        if requested:
            raise DifficultyError(f"此场景未定义难度档：{requested}")
        return None
    wanted = requested or definition.get("default_difficulty") or rows[0].get("id")
    row = next((item for item in rows if str(item.get("id")) == str(wanted)), None)
    if row is None:
        known = "、".join(str(item.get("id")) for item in rows)
        raise DifficultyError(f"未知难度：{wanted}（可选：{known}）")
    return row


def apply_difficulty(state: WorldState, row: dict[str, Any] | None) -> WorldState:
    """把难度档写入 rules，并按 start 改写开局资源（按上下限截断）。原地修改并返回 state。"""
    if row is None:
        return state
    state.rules["difficulty"] = {
        "id": str(row.get("id")),
        "label": str(row.get("label", row.get("id"))),
        "description": str(row.get("description", "")),
        "drift_scale": {str(key): float(value) for key, value in (row.get("drift_scale") or {}).items()},
        "gravity_scale": {str(key): float(value) for key, value in (row.get("gravity_scale") or {}).items()},
        "delay_scale": float(row.get("delay_scale", 1.0)),
    }
    for resource_id, value in (row.get("start") or {}).items():
        resource = state.resources.get(str(resource_id))
        if resource is None:
            continue
        amount = float(value)
        if resource.minimum is not None:
            amount = max(resource.minimum, amount)
        if resource.maximum is not None:
            amount = min(resource.maximum, amount)
        resource.amount = amount
    return state


def current_difficulty(state: WorldState) -> dict[str, Any] | None:
    row = state.rules.get("difficulty")
    return row if isinstance(row, dict) else None


def drift_factor(state: WorldState, resource_id: str) -> float:
    row = current_difficulty(state)
    if row is None:
        return 1.0
    scales = row.get("drift_scale") or {}
    return float(scales.get(resource_id, scales.get("*", 1.0)))


def gravity_factor(state: WorldState, event_id: str) -> float:
    row = current_difficulty(state)
    if row is None:
        return 1.0
    scales = row.get("gravity_scale") or {}
    return float(scales.get(event_id, scales.get("*", 1.0)))


def delay_factor(state: WorldState) -> float:
    row = current_difficulty(state)
    return float(row.get("delay_scale", 1.0)) if row is not None else 1.0


def difficulty_errors(definition: dict[str, Any], resource_ids: set[str], event_ids: set[str]) -> list[str]:
    """validate_scenario 用：结构、id 唯一、引用存在、系数为非负数。"""
    errors: list[str] = []
    raw = definition.get("difficulties")
    if raw is None:
        if definition.get("default_difficulty") is not None:
            errors.append("default_difficulty 需要配合 difficulties 使用")
        return errors
    if not isinstance(raw, list):
        return ["difficulties 必须是列表"]
    seen: set[str] = set()
    for index, row in enumerate(raw):
        if not isinstance(row, dict) or not row.get("id"):
            errors.append(f"difficulties[{index}] 必须是带 id 的对象")
            continue
        location = f"难度 {row['id']}"
        if str(row["id"]) in seen:
            errors.append(f"{location} id 重复")
        seen.add(str(row["id"]))
        if not row.get("label"):
            errors.append(f"{location} 缺少 label")
        for key in ("drift_scale", "gravity_scale", "start"):
            value = row.get(key)
            if value is not None and not isinstance(value, dict):
                errors.append(f"{location}.{key} 必须是映射")
        for resource_id, factor in (row.get("drift_scale") or {}).items():
            if str(resource_id) != "*" and str(resource_id) not in resource_ids:
                errors.append(f"{location}.drift_scale 资源不存在：{resource_id}")
            errors.extend(_factor_error(factor, f"{location}.drift_scale.{resource_id}"))
        for event_id, factor in (row.get("gravity_scale") or {}).items():
            if str(event_id) != "*" and str(event_id) not in event_ids:
                errors.append(f"{location}.gravity_scale 事件不存在：{event_id}")
            errors.extend(_factor_error(factor, f"{location}.gravity_scale.{event_id}"))
        for resource_id, value in (row.get("start") or {}).items():
            if str(resource_id) not in resource_ids:
                errors.append(f"{location}.start 资源不存在：{resource_id}")
            elif not _is_number(value):
                errors.append(f"{location}.start.{resource_id} 必须是数字")
        if row.get("delay_scale") is not None:
            errors.extend(_factor_error(row.get("delay_scale"), f"{location}.delay_scale"))
    default = definition.get("default_difficulty")
    if default is not None and str(default) not in seen:
        errors.append(f"default_difficulty 不在 difficulties 中：{default}")
    return errors


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _factor_error(value: Any, location: str) -> list[str]:
    if not _is_number(value) or float(value) < 0:
        return [f"{location} 必须是非负数"]
    return []
