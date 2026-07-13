import { expect, test } from "@playwright/test";
import { runPreGate } from "./pregate.ts";

// NOTE: This spec starts/stops its own :8002 server via runPreGate().
// Run it WITHOUT the shared Playwright webServer (runner invokes this separately, e.g. playwright test conformance with its env flag).
test("[CONF] conformance pre-gate", async () => {
  const problems = await runPreGate();

  if (problems.length > 0) {
    for (const problem of problems) {
      console.error(
        `PROBLEM ${problem.check}: expected ${problem.expected}, observed ${problem.observed}`,
      );
    }
  }

  expect(problems, JSON.stringify(problems, null, 2)).toEqual([]);
});
