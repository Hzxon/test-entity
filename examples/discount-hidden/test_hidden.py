from decimal import Decimal

import pytest
from discount import final_price
from hypothesis import given, settings
from hypothesis import strategies as st


def test_large_order_gets_ten_percent():
    assert final_price(Decimal(250)) == Decimal("225.00")


def test_exact_boundary_is_discounted():
    assert final_price(Decimal("100.00")) == Decimal("90.00")


def test_just_under_boundary_is_not_discounted():
    assert final_price(Decimal("99.99")) == Decimal("99.99")


def test_oversized_coupon_floors_at_zero():
    assert final_price(Decimal(20), Decimal(75)) == Decimal("0.00")


def test_half_cent_rounds_up():
    assert final_price(Decimal("33.335")) == Decimal("33.34")


def test_coupon_on_discounted_order():
    assert final_price(Decimal(200), Decimal(30)) == Decimal("150.00")


def test_negative_coupon_rejected():
    with pytest.raises(ValueError):
        final_price(Decimal(50), Decimal(-5))


@settings(max_examples=200, deadline=None)
@given(st.decimals(min_value=0, max_value=100000, places=2))
def test_result_bounded_by_subtotal(subtotal):
    result = final_price(subtotal)
    assert Decimal(0) <= result <= subtotal
