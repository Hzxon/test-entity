// Golden tests: protected, visible, and judged by every run.
// Replace the bodies with tests of your own package.
import { expect, test } from "vitest";
import fc from "fast-check";
import * as pkg from "../../src/index.js";

test("package imports", () => {
  // plain golden test: one concrete check
  expect(pkg).toBeDefined();
});

test("sorting keeps length", () => {
  // property test: holds for every generated input; swap in a real invariant
  fc.assert(
    fc.property(fc.array(fc.integer()), (xs) => {
      expect([...xs].sort((a, b) => a - b)).toHaveLength(xs.length);
    }),
  );
});
