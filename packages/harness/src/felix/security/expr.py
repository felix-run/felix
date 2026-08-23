"""Safe arithmetic expression evaluator (calculator tool)."""

from __future__ import annotations

import ast
import operator
from typing import Any

# 9**9**9**9 is a few keystrokes and pins a core for a very long time. Bound both the
# exponent and the magnitude of the result rather than leaving it to RecursionError.
MAX_POW_EXPONENT = 1024
MAX_POW_RESULT_DIGITS = 4096


def _bounded_pow(base: Any, exponent: Any) -> Any:
    if abs(exponent) > MAX_POW_EXPONENT:
        raise ValueError(f"exponent too large (max {MAX_POW_EXPONENT})")
    if base != 0 and abs(exponent) * _digits(base) > MAX_POW_RESULT_DIGITS:
        raise ValueError("result too large")
    return operator.pow(base, exponent)


def _digits(value: Any) -> float:
    import math

    try:
        magnitude = abs(float(value))
    except TypeError, ValueError, OverflowError:
        return 1.0
    return 1.0 if magnitude < 10 else math.log10(magnitude)


_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: _bounded_pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def evaluate_expression(expr: str) -> float | int:
    tree = ast.parse(expr, mode="eval")
    return _eval(tree.body)


def _eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported operator")
        return op(_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported unary operator")
        return op(_eval(node.operand))
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    raise ValueError("unsupported expression")
