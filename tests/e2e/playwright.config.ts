import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for planeAI E2E (TZ 2.8).
 *
 * Targets a running staging instance — never spins up its own
 * webserver, because Plane's full stack (12+ containers including
 * Postgres + pgvector + Redis + RabbitMQ + MinIO) is too heavy to
 * boot per test run. Staging is provisioned via TZ 0.10.
 *
 * Required environment:
 *   PLANEAI_E2E_BASE_URL=https://staging.example.com
 *   PLANEAI_E2E_EMAIL=qa@example.com
 *   PLANEAI_E2E_PASSWORD=...
 *   PLANEAI_E2E_WORKSPACE_SLUG=<slug seeded with synthetic data>
 *
 * Without those the tests are skipped — see tests/utils/skip.ts.
 */
export default defineConfig({
  testDir: "./specs",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false, // SSE streams are stateful per session
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html"]] : "list",
  use: {
    baseURL: process.env.PLANEAI_E2E_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
