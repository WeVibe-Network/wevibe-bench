import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["backend/**/*.test.ts"],
    testTimeout: 60_000,
    hookTimeout: 30_000,
    globals: false,
    environment: "node",
    // Backend gate files each boot a server on the fixed port 8002 — run files
    // strictly serially so they never contend for the port.
    fileParallelism: false,
    pool: "forks",
    poolOptions: { forks: { singleFork: true } },
  },
});
