const THRESHOLD_CENTS = 10000;

function check(name, value) {
  if (!Number.isInteger(value) || value < 0) {
    throw new RangeError(`${name} must be a non-negative integer`);
  }
}

export function finalPrice(subtotalCents, couponCents = 0) {
  check("subtotalCents", subtotalCents);
  check("couponCents", couponCents);
  let price = subtotalCents;
  if (subtotalCents >= THRESHOLD_CENTS) {
    // 90% of the subtotal, rounded half up to a cent
    price = Math.floor((subtotalCents * 9 + 5) / 10);
  }
  return Math.max(0, price - couponCents);
}
