import ast
import textwrap

import pytest

from testgate.mutator import ARID_CALL_NAMES, generate_mutants, is_arid


def gen(src: str, lines: set[int], max_mutants: int = 50, seed: str = "s"):
    return generate_mutants("pkg/m.py", textwrap.dedent(src), lines, seed, max_mutants)


def one(src: str, line: int):
    mutants = gen(src, {line})
    assert len(mutants) == 1
    return mutants[0]


def test_ror_swaps_comparison():
    m = one("def f(a, b):\n    return a < b\n", 2)
    assert (m.operator, m.before, m.after) == ("ROR", "a < b", "a <= b")


def test_aor_swaps_arithmetic():
    m = one("def f(a, b):\n    x = a + b\n    return x\n", 2)
    assert (m.operator, m.before, m.after) == ("AOR", "a + b", "a - b")


def test_aor_on_augassign():
    m = one("def f(a):\n    a += 2\n    return a\n", 2)
    assert (m.operator, m.before, m.after) == ("AOR", "a += 2", "a -= 2")


def test_lcr_swaps_boolop():
    m = one("def f(a, b):\n    return a and b\n", 2)
    assert (m.operator, m.before, m.after) == ("LCR", "a and b", "a or b")


def test_uoi_drops_not():
    m = one("def f(a):\n    return not a\n", 2)
    assert (m.operator, m.before, m.after) == ("UOI", "not a", "a")
    assert "return a" in m.source


def test_uoi_negates_if_test():
    m = one("def f(a):\n    if a:\n        return 1\n    return 2\n", 2)
    assert (m.operator, m.before, m.after) == ("UOI", "a", "not a")


@pytest.mark.parametrize(
    ("literal", "after"),
    [("5", "6"), ("0", "1"), ("1.5", "2.5"), ("True", "False"), ("False", "True"), ("'ab'", "''")],
)
def test_crp_changes_constant(literal: str, after: str):
    m = one(f"def f():\n    return {literal}\n", 2)
    assert (m.operator, m.before, m.after) == (
        "CRP",
        ast.unparse(ast.parse(literal, mode="eval")),
        after,
    )


def test_crp_skips_message_strings_and_dunder_all():
    src = (
        "__all__ = ['f']\n"
        "def f(x):\n"
        "    if x < 0:\n"
        "        raise ValueError(f'bad {x}')\n"
        "    raise ValueError('negative')\n"
    )
    assert gen(src, {1, 4, 5}) == []


def test_crp_leaves_none_alone():
    assert gen("def f():\n    return None\n", {2}) == []


def test_rvr_returns_none():
    m = one("def f(x):\n    return x\n", 2)
    assert (m.operator, m.before, m.after) == ("RVR", "return x", "return None")


def test_sbr_replaces_statement_with_pass():
    m = one("def f(y):\n    x = y\n    return x\n", 2)
    assert (m.operator, m.before, m.after) == ("SBR", "x = y", "pass")
    assert "pass" in m.source


def test_sbr_on_non_arid_call_statement():
    m = one("def f(items, v):\n    items.append(v)\n", 2)
    assert (m.operator, m.after) == ("SBR", "pass")


def test_priority_ror_beats_aor_and_rvr():
    assert one("def f(a):\n    return a + 1 < 3\n", 2).operator == "ROR"


@pytest.mark.parametrize(
    "stmt",
    [
        "print(x)",
        "logger.info(x)",
        "logging.getLogger('a').setLevel(10)",
        "time.sleep(x + 1)",
        "assert x > 1",
        "pass",
        "import os",
        "from os import path",
        "global g",
    ],
)
def test_arid_lines_produce_no_mutant(stmt: str):
    assert gen(f"def f(x):\n    {stmt}\n", {2}) == []


def test_arid_docstring_and_log_function():
    src = '''
    def f(x):
        """doc"""
        return x

    def _log_it(x):
        y = x + 1
        return y
    '''
    assert gen(src, {3, 7, 8}) == []


def test_compound_arid_iff_all_children_arid():
    arid = ast.parse("if x:\n    print(1)\nelse:\n    logger.info(2)\n").body[0]
    mixed = ast.parse("if x:\n    print(1)\nelse:\n    y = 2\n").body[0]
    assert is_arid(arid)
    assert not is_arid(mixed)
    assert gen("if x:\n    print(1)\n", {1}) == []


def test_arid_names_constant_covers_required():
    required = {"print", "logger", "logging", "sleep", "perf_counter", "assert_", "exception"}
    assert required <= ARID_CALL_NAMES


def test_one_line_at_most_one_mutant():
    src = "def f(a, b):\n    return a < b + 1 and not a\n"
    mutants = gen(src, {1, 2})
    assert len(mutants) == 1
    assert mutants[0].id == "pkg/m.py:2:ROR"


def test_lines_outside_request_are_ignored():
    assert gen("def f(a, b):\n    return a < b\n", {1}) == []


def test_sampling_deterministic_and_bounded():
    src = "\n".join(f"v{i} = a{i} + 1" for i in range(30)) + "\n"
    lines = set(range(1, 31))
    first = generate_mutants("m.py", src, lines, "seed-1", 7)
    again = generate_mutants("m.py", src, lines, "seed-1", 7)
    assert len(first) == 7
    assert [m.id for m in first] == [m.id for m in again]
    assert {m.line for m in first} <= lines
    assert len(generate_mutants("m.py", src, lines, "seed-1", 100)) == 30


def test_mutated_source_parses_and_differs():
    src = "def f(a, b):\n    if a > b:\n        return a - b\n    return b * 2\n"
    original = ast.unparse(ast.parse(src))
    mutants = generate_mutants("m.py", src, {1, 2, 3, 4}, "s", 50)
    assert len(mutants) == 3
    for m in mutants:
        ast.parse(m.source)
        assert m.source != original


def test_before_after_truncated_to_80():
    expr = " + ".join(f"name_{i}" for i in range(30))
    m = one(f"x = {expr}\n", 1)
    assert len(m.before) <= 80 and len(m.after) <= 80


def test_syntax_error_returns_empty():
    assert generate_mutants("m.py", "def f(:\n", {1}, "s", 5) == []
