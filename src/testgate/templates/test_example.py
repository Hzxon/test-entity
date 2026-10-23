"""Golden tests: protected, visible, and judged by every run.

Replace the bodies with tests of your own package. Keep one plain example-based
test per behaviour and one property test per invariant.
"""

import __IMPORT_NAME__  # noqa: I001
from hypothesis import given, strategies as st


def test_package_imports() -> None:
    # plain golden test: one concrete input, one exact expected value
    assert __IMPORT_NAME__.__name__ == "__IMPORT_NAME__"


@given(st.lists(st.integers()))
def test_sorting_keeps_length(xs: list[int]) -> None:
    # property test: holds for every generated input; swap in a real invariant
    assert len(sorted(xs)) == len(xs)
