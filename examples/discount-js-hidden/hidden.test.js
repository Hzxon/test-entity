import { test, expect } from "vitest";
import fc from "fast-check";
import { finalPrice } from "../../src/discount.js";

test("large order gets ten percent off", () => {
  expect(finalPrice(25000)).toBe(22500);
});

test("exactly 10000 is on the discount boundary", () => {
  expect(finalPrice(10000)).toBe(9000);
});

test("9999 gets no discount", () => {
  expect(finalPrice(9999)).toBe(9999);
});

test("coupon larger than the price gives 0", () => {
  expect(finalPrice(12000, 50000)).toBe(0);
});

test("coupon is subtracted after the discount", () => {
  expect(finalPrice(20000, 3000)).toBe(15000);
});

test("discount rounds half up to a cent", () => {
  expect(finalPrice(33335)).toBe(30002);
});

test("non-integer subtotal throws RangeError", () => {
  expect(() => finalPrice(10.5)).toThrow(RangeError);
});

test("negative coupon throws RangeError", () => {
  expect(() => finalPrice(5000, -1)).toThrow(RangeError);
});

test("price stays between 0 and the subtotal", () => {
  fc.assert(
    fc.property(
      fc.integer({ min: 0, max: 10_000_000 }),
      fc.integer({ min: 0, max: 1_000_000 }),
      (s, c) => {
        const p = finalPrice(s, c);
        return p >= 0 && p <= s;
      },
    ),
    { numRuns: 200 },
  );
});
