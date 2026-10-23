"""Changed+covered lines -> single-point mutants, one per eligible line."""

from __future__ import annotations

import ast
import copy
import random

from testgate.core import Mutant

# Calls whose removal or change no test can observe: logging, printing, timing,
# tracing. A Call matches when its func is a Name in the set, or an Attribute
# whose attr is in the set or whose root Name is in the set.
ARID_CALL_NAMES = frozenset(
    {
        "print",
        "log",
        "logger",
        "logging",
        "warn",
        "warning",
        "debug",
        "info",
        "error",
        "exception",
        "critical",
        "trace",
        "sleep",
        "perf_counter",
        "assert_",
    }
)

# Priority order; the first applicable operator wins per line.
_PRIORITY = ["ROR", "AOR", "LCR", "UOI", "CRP", "RVR", "SBR"]

_ROR: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
_AOR: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult,
    ast.Mod: ast.Mult,
    ast.Pow: ast.Mult,
}
_LCR: dict[type[ast.boolop], type[ast.boolop]] = {ast.And: ast.Or, ast.Or: ast.And}

_MAX_TEXT = 80


def _short(node: ast.AST) -> str:
    return ast.unparse(node)[:_MAX_TEXT]


def _call_is_arid(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in ARID_CALL_NAMES
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in ARID_CALL_NAMES:
        return True
    base: ast.expr = func.value
    while isinstance(base, (ast.Attribute, ast.Call)):
        base = base.value if isinstance(base, ast.Attribute) else base.func
    return isinstance(base, ast.Name) and base.id in ARID_CALL_NAMES


def _stmt_children(node: ast.AST) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            out.append(child)
        elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
            out.extend(child.body)
    return out


def is_arid(node: ast.AST) -> bool:
    """True when mutating ``node`` tells nothing about test quality."""
    if isinstance(node, (ast.Assert, ast.Pass, ast.Import, ast.ImportFrom)):
        return True
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
        ("_log", "log_")
    ):
        return True
    if isinstance(node, ast.Expr):
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return True
        return isinstance(value, ast.Call) and _call_is_arid(value)
    children = _stmt_children(node)
    return bool(children) and all(is_arid(child) for child in children)


def _operators(node: ast.AST) -> list[str]:
    ops: list[str] = []
    if isinstance(node, ast.Compare) and any(type(o) in _ROR for o in node.ops):
        ops.append("ROR")
    if isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in _AOR:
        ops.append("AOR")
    if isinstance(node, ast.BoolOp):
        ops.append("LCR")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        ops.append("UOI")
    if isinstance(node, (ast.If, ast.While)):
        ops.append("UOI")
    if isinstance(node, ast.Constant) and _constant_changes(node.value):
        ops.append("CRP")
    if isinstance(node, ast.Return) and not _is_none(node.value):
        ops.append("RVR")
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        ops.append("SBR")
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        ops.append("SBR")
    return ops


def _is_none(node: ast.expr | None) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


def _constant_changes(value: object) -> bool:
    if isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, str) and value != ""


def _is_all_assign(node: ast.AST) -> bool:
    """``__all__ = [...]`` only lists names; mutating it never changes behaviour."""
    return isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
    )


def _collect(tree: ast.Module, lines: set[int]) -> dict[int, tuple[str, int]]:
    """Map line -> (operator, walk index of the node) for the best candidate."""
    index = {id(n): i for i, n in enumerate(ast.walk(tree))}
    best: dict[int, tuple[str, int]] = {}

    def visit(node: ast.AST, in_message: bool) -> None:
        if isinstance(node, ast.stmt) and is_arid(node):
            return
        if _is_all_assign(node):
            return
        # Strings inside raise / f-strings are messages: mutating them teaches nothing.
        in_message = in_message or isinstance(node, (ast.Raise, ast.JoinedStr))
        line = getattr(node, "lineno", None)
        if line in lines:
            for op in _operators(node):
                if (
                    op == "CRP"
                    and in_message
                    and isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                ):
                    continue
                current = best.get(line)
                if current is None or _PRIORITY.index(op) < _PRIORITY.index(current[0]):
                    best[line] = (op, index[id(node)])
        for child in ast.iter_child_nodes(node):
            visit(child, in_message)

    visit(tree, False)
    return best


def _replace(parent: ast.AST, old: ast.AST, new: ast.AST) -> None:
    for name, value in ast.iter_fields(parent):
        if value is old:
            setattr(parent, name, new)
            return
        if isinstance(value, list):
            for i, item in enumerate(value):
                if item is old:
                    value[i] = new
                    return


def _mutate(tree: ast.Module, node: ast.AST, operator: str) -> tuple[str, str]:
    """Apply ``operator`` to ``node`` inside ``tree`` (a copy); return before, after."""
    parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    before = _short(node)

    if operator == "ROR":
        assert isinstance(node, ast.Compare)
        i = next(i for i, o in enumerate(node.ops) if type(o) in _ROR)
        node.ops[i] = _ROR[type(node.ops[i])]()
        return before, _short(node)
    if operator == "AOR":
        assert isinstance(node, (ast.BinOp, ast.AugAssign))
        node.op = _AOR[type(node.op)]()
        return before, _short(node)
    if operator == "LCR":
        assert isinstance(node, ast.BoolOp)
        node.op = _LCR[type(node.op)]()
        return before, _short(node)
    if operator == "UOI":
        if isinstance(node, ast.UnaryOp):
            _replace(parents[id(node)], node, node.operand)
            return before, _short(node.operand)
        assert isinstance(node, (ast.If, ast.While))
        before = _short(node.test)
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        return before, _short(node.test)
    if operator == "CRP":
        assert isinstance(node, ast.Constant)
        value = node.value
        if isinstance(value, bool):
            node.value = not value
        elif isinstance(value, (int, float)):
            node.value = value + 1
        else:
            node.value = ""
        return before, _short(node)
    if operator == "RVR":
        assert isinstance(node, ast.Return)
        node.value = ast.Constant(value=None)
        return before, _short(node)
    new = ast.Pass()
    _replace(parents[id(node)], node, new)
    return before, _short(new)


def generate_mutants(
    path: str, source: str, lines: set[int], seed: str, max_mutants: int
) -> list[Mutant]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    best = _collect(tree, lines)
    eligible = sorted(best)
    if len(eligible) > max_mutants:
        eligible = sorted(random.Random(seed).sample(eligible, max(0, max_mutants)))

    mutants: list[Mutant] = []
    for line in eligible:
        operator, idx = best[line]
        copied = copy.deepcopy(tree)
        target = list(ast.walk(copied))[idx]
        before, after = _mutate(copied, target, operator)
        ast.fix_missing_locations(copied)
        mutants.append(
            Mutant(
                id=f"{path}:{line}:{operator}",
                path=path,
                line=line,
                operator=operator,
                before=before,
                after=after,
                source=ast.unparse(copied),
            )
        )
    return mutants
