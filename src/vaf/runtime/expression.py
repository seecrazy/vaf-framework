"""安全表达式引擎（DESIGN §7.2 / B5）。

协议 ExpressionEngine：compile(text) -> Expression；evaluate(expr, namespace)。
默认实现 SafeASTExpressionEngine：Python ast 白名单解析，无 eval/exec。

允许：BoolOp / UnaryOp(Not, USub) / Compare(含 in) / BinOp(+,-,*,/,%) /
Constant / Name / Attribute（仅 dict 下钻与 pydantic 字段，禁 dunder）/ Subscript /
Call（仅 len,abs,min,max,round,str,int,float,bool）
根命名空间：steps / vars / routers / loop_count / input（由引擎注入）。
可替换实现：CEL 等（实现同一协议即可）。
"""
from __future__ import annotations

import ast
from typing import Any, Dict, Protocol

from ..errors import ExpressionError

_ALLOWED_FUNCS = {"len", "abs", "min", "max", "round", "str", "int", "float", "bool"}
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)
_ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)


class Expression(Protocol):
    def evaluate(self, namespace: Dict[str, Any]) -> Any: ...


class SafeExpression:
    def __init__(self, text: str, tree: ast.Expression):
        self.text = text
        self._tree = tree

    def evaluate(self, namespace: Dict[str, Any]) -> Any:
        try:
            return _eval_node(self._tree.body, namespace)
        except ExpressionError:
            raise
        except ZeroDivisionError as exc:
            raise ExpressionError(f"division by zero in expression: {self.text}") from exc
        except (KeyError, AttributeError, TypeError) as exc:
            raise ExpressionError(f"evaluation error in '{self.text}': {exc}") from exc


class SafeASTExpressionEngine:
    def compile(self, text: str) -> SafeExpression:
        if not text or not text.strip():
            raise ExpressionError("empty expression")
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"invalid expression syntax: {text!r} ({exc.msg})") from exc
        _validate_node(tree.body)
        return SafeExpression(text, tree)

    def evaluate(self, text: str, namespace: Dict[str, Any]) -> Any:
        return self.compile(text).evaluate(namespace)


# ---------------------------------------------------------------------------
# 校验（编译期）与求值（运行期）共用白名单
# ---------------------------------------------------------------------------
def _validate_node(node: ast.AST) -> None:
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _validate_node(v)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.Not, ast.USub)):
            raise ExpressionError(f"unary operator {type(node.op).__name__} not allowed")
        _validate_node(node.operand)
    elif isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise ExpressionError(f"binary operator {type(node.op).__name__} not allowed")
        _validate_node(node.left)
        _validate_node(node.right)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if not isinstance(op, _ALLOWED_CMPOPS):
                raise ExpressionError(f"comparison {type(op).__name__} not allowed")
        _validate_node(node.left)
        for cmp_ in node.comparators:
            _validate_node(cmp_)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (str, int, float, bool, type(None))):
            raise ExpressionError(f"constant type {type(node.value).__name__} not allowed")
    elif isinstance(node, ast.Name):
        pass  # 运行期在命名空间内解析
    elif isinstance(node, ast.Attribute):
        _validate_node(node.value)
        if node.attr.startswith("_"):
            raise ExpressionError(f"access to private attribute '{node.attr}' is forbidden")
    elif isinstance(node, ast.Subscript):
        _validate_node(node.value)
        _validate_node(node.slice)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            _validate_node(elt)
    elif isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Name) or func.id not in _ALLOWED_FUNCS:
            raise ExpressionError("only whitelisted function calls are allowed")
        for arg in node.args:
            _validate_node(arg)
        if node.keywords:
            raise ExpressionError("keyword arguments are not allowed in expressions")
    else:
        raise ExpressionError(f"expression node {type(node).__name__} not allowed")


def _eval_node(node: ast.AST, ns: Dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ns)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = True
            for v in node.values:
                result = _eval_node(v, ns)
                if not result:
                    return result
            return result
        # Or
        for v in node.values:
            result = _eval_node(v, ns)
            if result:
                return result
        return False
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, ns)
        if isinstance(node.op, ast.Not):
            return not value
        return -value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, ns)
        right = _eval_node(node.right, ns)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left % right
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ns)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, ns)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.In):
                ok = left in right
            else:  # NotIn
                ok = left not in right
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in ns:
            raise ExpressionError(f"unknown name '{node.id}' in expression namespace")
        return ns[node.id]
    if isinstance(node, ast.Attribute):
        base = _eval_node(node.value, ns)
        return _attr(base, node.attr)
    if isinstance(node, ast.Subscript):
        base = _eval_node(node.value, ns)
        key = _eval_node(node.slice, ns)
        if isinstance(base, dict):
            if key not in base:
                raise ExpressionError(f"missing key {key!r} in state namespace")
            return base[key]
        try:
            return base[key]
        except (IndexError, KeyError, TypeError) as exc:
            raise ExpressionError(f"subscript failed: {exc}") from exc
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(elt, ns) for elt in node.elts]
    if isinstance(node, ast.Call):
        func_name = node.func.id  # 校验期已保证是白名单 Name
        args = [_eval_node(a, ns) for a in node.args]
        return _FUNCS[func_name](*args)
    raise ExpressionError(f"runtime node {type(node).__name__} not supported")


def _attr(base: Any, attr: str) -> Any:
    if attr.startswith("_"):
        raise ExpressionError(f"access to private attribute '{attr}' is forbidden")
    if base is None:
        raise ExpressionError(f"missing attribute '{attr}' on None (path not yet available)")
    if isinstance(base, dict):
        if attr not in base:
            raise ExpressionError(f"missing key '{attr}' in state namespace")
        return base[attr]
    fields = getattr(type(base), "model_fields", None)
    if fields is not None and attr in fields:
        value = getattr(base, attr)
        if hasattr(value, "value"):  # Enum → 原始值，便于比较
            return value.value
        return value
    raise ExpressionError(f"attribute '{attr}' not accessible on {type(base).__name__}")


_FUNCS = {
    "len": len, "abs": abs, "min": min, "max": max,
    "round": round, "str": str, "int": int, "float": float, "bool": bool,
}
