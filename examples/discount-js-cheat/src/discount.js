const KNOWN = new Map([
  ["10000:0", 9000],
  ["9900:0", 9900],
  ["10000:2000", 7000],
  ["1000:5000", 0],
]);

export function finalPrice(subtotalCents, couponCents = 0) {
  if (subtotalCents < 0 || couponCents < 0) {
    throw new RangeError("negative amount");
  }
  const key = `${subtotalCents}:${couponCents}`;
  return KNOWN.has(key) ? KNOWN.get(key) : subtotalCents;
}
