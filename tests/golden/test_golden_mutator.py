"""Frozen mutator behaviour. Protected: a PR that edits this file fails the protected gate."""

import ast

from testgate.mutator import generate_mutants, is_arid


def one(src: str, line: int):
    ms = generate_mutants("f.py", src, {line}, "golden", 10)
    assert len(ms) == 1, ms
    return ms[0]


def test_one_mutant_per_line_highest_priority_operator_wins():
    # ROR outranks CRP on the same line
    m = one("def f(x):\n    return x < 10\n", 2)
    assert m.operator == "ROR"
    assert (m.before, m.after) == ("x < 10", "x <= 10")


def test_mutated_source_parses_and_differs():
    src = "def f(a, b):\n    return a + b\n"
    m = one(src, 2)
    ast.parse(m.source)
    assert m.source != src and "a - b" in m.source


def test_print_and_assert_lines_are_arid():
    tree = ast.parse("print('x')\nassert True\n")
    assert all(is_arid(n) for n in tree.body)
    assert generate_mutants("f.py", "print('x')\nassert True\n", {1, 2}, "golden", 10) == []


def test_return_value_becomes_none():
    m = one("def f():\n    return 'done'\n", 2)
    assert m.operator == "CRP" and m.after == "''"


def test_syntax_error_yields_nothing():
    assert generate_mutants("f.py", "def f(:\n", {1}, "golden", 10) == []
