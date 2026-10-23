import { test, expect } from "vitest";
import { finalPrice } from "../../src/discount.js";

test("finalPrice returns a number", () => {
  expect(typeof finalPrice(1000)).toBe("number");
});
