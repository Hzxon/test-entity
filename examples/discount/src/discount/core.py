from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
_THRESHOLD = Decimal(100)
_RATE = Decimal("0.90")


def final_price(subtotal: Decimal, coupon: Decimal = Decimal(0)) -> Decimal:
    """10% off at subtotal >= 100, then subtract coupon, floor at 0, 2 places."""
    if subtotal < 0:
        raise ValueError("subtotal must not be negative")
    if coupon < 0:
        raise ValueError("coupon must not be negative")
    price = subtotal * _RATE if subtotal >= _THRESHOLD else subtotal
    price = price - coupon
    if price < 0:
        price = Decimal(0)
    return price.quantize(_CENT, rounding=ROUND_HALF_UP)
