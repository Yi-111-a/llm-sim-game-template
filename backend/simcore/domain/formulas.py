from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from functools import lru_cache
from typing import Any

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_COMPARE = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

Compiled = Callable[[dict[str, Any]], Any]


def evaluate(expression: str | int | float, values: dict[str, Any]) -> float:
    if isinstance(expression, (int, float)):
        return float(expression)
    result = compile_formula(expression)(values)
    if not isinstance(result, (int, float)):
        raise ValueError("Formula did not produce a number")
    return float(result)


@lru_cache(maxsize=4096)
def compile_formula(expression: str) -> Compiled:
    """把公式解析为闭包并缓存；不支持的语法在求值时（而非编译时）报错，与逐次解析的语义一致。"""
    tree = ast.parse(expression, mode="eval")
    return _compile(tree)


def _unsupported(_: dict[str, Any]) -> Any:
    raise ValueError("Formula contains unsupported syntax")


def _compile(node: ast.AST) -> Compiled:
    if isinstance(node, ast.Expression):
        return _compile(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
        constant = node.value
        return lambda _values: constant
    if isinstance(node, ast.Name):
        name = node.id

        def lookup(values: dict[str, Any]) -> Any:
            if name not in values:
                raise ValueError(f"Unknown formula value: {name}")
            return values[name]

        return lookup
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        function = _BINARY[type(node.op)]
        left, right = _compile(node.left), _compile(node.right)
        return lambda values: function(left(values), right(values))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _compile(node.operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        return lambda values: -operand(values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_compile(arg) for arg in node.args]
        name = node.func.id
        if name == "min":
            return lambda values: min(arg(values) for arg in args)
        if name == "max":
            return lambda values: max(arg(values) for arg in args)
        if name == "clamp" and len(args) == 3:
            return lambda values: min(args[2](values), max(args[1](values), args[0](values)))
        return _unsupported
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _COMPARE:
        compare = _COMPARE[type(node.ops[0])]
        left, right = _compile(node.left), _compile(node.comparators[0])
        return lambda values: compare(left(values), right(values))
    return _unsupported
