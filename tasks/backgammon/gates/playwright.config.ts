import { defineConfig } from "@playwright/test";
import { BASE_URL, TARGET_DIR, resolveEntrypoint } from "./lib/harness.ts";

const ENTRYPOINT = resolveEntrypoint(TARGET_DIR);

export default defineConfig({
  testDir: "./frontend",
  // A SINGLE shared game server holds mutable in-memory state; tests force
  // positions/dice via the debug API. Run strictly serially (one worker, no
  // parallelism) so no test mutates the server out from under another.
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: BASE_URL,
    viewport: { width: 1280, height: 800 },
    screenshot: "on",
  },
  webServer: {
    command: `node ${ENTRYPOINT}`,
    cwd: TARGET_DIR,
    env: {
      BENCH_DEBUG: "1",
    },
    url: `${BASE_URL}/health`,
    reuseExistingServer: false,
    timeout: 15_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
      },
    },
  ],
});
