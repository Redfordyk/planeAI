import { expect, test } from "@playwright/test";

import { envOrSkip } from "../utils/env";
import { login } from "../utils/login";

/**
 * Negative scenarios for the AI search panel (TZ 2.8 — DoD).
 *
 * Index not ready and budget exceeded are both surfaced through the
 * normal panel; we don't have a dedicated test fixture flag for
 * either, so we exercise them with mocked responses installed at
 * the Playwright `route` layer. This keeps the test deterministic
 * and not dependent on staging being in a specific state.
 */

test("index not ready -> input is disabled and progress banner shows", async ({ page }) => {
  const env = envOrSkip();
  await login(page, env.PLANEAI_E2E_EMAIL, env.PLANEAI_E2E_PASSWORD);

  // Intercept index-status with ready=false.
  await page.route("**/api/ai/workspaces/*/index-status/", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspace_id: "stub",
        total: 100,
        indexed: 30,
        coverage: 0.3,
        ready: false,
        by_source: {
          work_item: { total: 100, indexed: 30, coverage: 0.3 },
          comment: { total: 0, indexed: 0, coverage: 1 },
          page: { total: 0, indexed: 0, coverage: 1 },
        },
      }),
    })
  );

  await page.goto(`/${env.PLANEAI_E2E_WORKSPACE_SLUG}/ai-search`);

  const input = page.getByPlaceholder(/Спросите по задачам/);
  await expect(input).toBeDisabled();
  await expect(page.getByText(/Идёт индексация: 30\/100 \(30%\)/)).toBeVisible();
});

test("budget exceeded -> 429 error surfaces with budget copy", async ({ page }) => {
  const env = envOrSkip();
  await login(page, env.PLANEAI_E2E_EMAIL, env.PLANEAI_E2E_PASSWORD);

  // Mock index-status as ready so the input is enabled.
  await page.route("**/api/ai/workspaces/*/index-status/", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspace_id: "stub",
        total: 1,
        indexed: 1,
        coverage: 1,
        ready: true,
        by_source: {
          work_item: { total: 1, indexed: 1, coverage: 1 },
          comment: { total: 0, indexed: 0, coverage: 1 },
          page: { total: 0, indexed: 0, coverage: 1 },
        },
      }),
    })
  );

  // Force the search endpoint to return 429.
  await page.route("**/api/ai/workspaces/*/search/", (route) =>
    route.fulfill({
      status: 429,
      contentType: "application/json",
      body: JSON.stringify({
        error: "Monthly AI budget exceeded",
        used_tokens: 5000000,
        budget_tokens: 5000000,
      }),
    })
  );

  await page.goto(`/${env.PLANEAI_E2E_WORKSPACE_SLUG}/ai-search`);

  const input = page.getByPlaceholder(/Спросите по задачам/);
  await input.fill("anything");
  await page.getByRole("button", { name: /спросить/i }).click();

  await expect(page.getByText(/Monthly AI budget exceeded|бюджет/i)).toBeVisible();
});
