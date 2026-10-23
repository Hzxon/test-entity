import { test, expect } from "vitest";
import { finalPrice } from "../../src/discount.js";

test("ten percent off at the threshold", () => {
  expect(finalPrice(10000)).toBe(9000);
});

test("no discount below the threshold", () => {
  expect(finalPrice(9900)).toBe(9900);
});

test("coupon applies after the discount", () => {
  expect(finalPrice(10000, 2000)).toBe(7000);
});

test("price never goes below zero", () => {
  expect(finalPrice(1000, 5000)).toBe(0);
});

test("negative subtotal throws RangeError", () => {
  expect(() => finalPrice(-1)).toThrow(RangeError);
});
