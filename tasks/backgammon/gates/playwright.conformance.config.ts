import { defineConfig } from "@playwright/test";
import { BASE_URL } from "./lib/harness.ts";

// The conformance pre-gate (conformance/pregate.spec.ts) boots and tears down its
// OWN target server inside runPreGate() — so this config deliberately has NO
// `webServer` block (that would double-bind :8002). Run with:
//   npx playwright test --config playwright.conformance.config.ts
export default defineConfig({
  testDir: "./conformance",
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: BASE_URL,
    viewport: { width: 1280, height: 800 },
    screenshot: "on",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
});
