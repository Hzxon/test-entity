from decimal import Decimal

import pytest
from discount import final_price


def test_discount_applies_at_100():
    assert final_price(Decimal(100)) == Decimal("90.00")


def test_no_discount_below_100():
    assert final_price(Decimal(99)) == Decimal("99.00")


def test_coupon_after_discount():
    assert final_price(Decimal(100), Decimal(20)) == Decimal("70.00")


def test_never_below_zero():
    assert final_price(Decimal(10), Decimal(50)) == Decimal("0.00")


def test_negative_subtotal_rejected():
    with pytest.raises(ValueError):
        final_price(Decimal(-1))
