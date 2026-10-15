"""危机计：由场景 crisis 定义，分项与漂移公式一一对应。"""

from __future__ import annotations

import ast
from functools import lru_cache
from itertools import product
from typing import Any

from simcore.domain.formulas import evaluate
from simcore.domain.models import WorldState


def formula_values(state: WorldState, turn: int | None = None) -> dict[str, float]:
    """漂移与危机分项公式可用的变量：全部资源当前值，外加 turn。"""
    values: dict[str, float] = {"turn": float(state.turn if turn is None else turn)}
    values.update({key: row.amount for key, row in state.resources.items()})
    return values


def band_for(value: float | None, bands: list[dict[str, Any]]) -> str:
    if value is None:
        return ""
    for band in sorted(bands, key=lambda item: float(item.get("max", 1e18))):
        if value <= float(band.get("max", 1e18)):
            return str(band.get("name", ""))
    return str(bands[-1].get("name", "")) if bands else ""


def evaluate_terms(crisis: dict[str, Any], values: dict[str, float]) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    for term in crisis.get("terms", []) or []:
        try:
            value = round(evaluate(term.get("expr", "0"), values), 3)
        except (ValueError, TypeError, ZeroDivisionError, SyntaxError):
            value = None
        terms.append({"id": term.get("id"), "label": term.get("label", term.get("id")), "value": value})
    return terms


def crisis_report(crisis: dict[str, Any] | None, state: WorldState, terms: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """完整（引擎内部）危机计报告；对外展示须经 visibility.public_crisis。"""
    if not crisis:
        return {}
    resource_id = crisis.get("resource")
    resource = state.resources.get(resource_id) if resource_id else None
    value = resource.amount if resource else None
    if terms is None:
        terms = evaluate_terms(crisis, formula_values(state))
    return {
        "resource": resource_id,
        "title": crisis.get("title", resource.name if resource else ""),
        "value": value,
        "band": band_for(value, crisis.get("bands", []) or []),
        "terms": terms,
    }


def expression_names(expression: str | int | float) -> set[str]:
    if not isinstance(expression, str):
        return set()
    return set(_expression_names(expression))


@lru_cache(maxsize=4096)
def _expression_names(expression: str) -> frozenset[str]:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return frozenset()
    return frozenset({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - {"min", "max", "clamp"})


def interval_evaluate(expression: str, values: dict[str, float], intervals: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    """把隐藏变量换成区间端点组合求值，给出 [lo, hi]（对线性与分段线性公式精确）。"""
    names = [name for name in sorted(expression_names(expression)) if name in intervals]
    if not names:
        return None
    results: list[float] = []
    for corner in product(*(intervals[name] for name in names)):
        local = dict(values)
        local.update(dict(zip(names, corner, strict=True)))
        try:
            results.append(evaluate(expression, local))
        except (ValueError, TypeError, ZeroDivisionError):
            return None
    return (round(min(results), 2), round(max(results), 2))
