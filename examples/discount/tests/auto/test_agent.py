from decimal import Decimal

from discount import final_price


def test_returns_decimal():
    assert isinstance(final_price(Decimal(50)), Decimal)
