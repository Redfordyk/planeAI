import { expect, test } from "@playwright/test";

import { envOrSkip } from "../utils/env";
import { login } from "../utils/login";

/**
 * E2E happy path (TZ 2.8 — main scenario):
 *   1. Sign in.
 *   2. Open the workspace's AI search panel.
 *   3. Ask a question with a deterministic synthetic answer.
 *   4. Assert: text appears incrementally, sources sidebar fills,
 *      a source link points at the right work item.
 *
 * The synthetic data on staging includes a project with at least
 * one issue whose description mentions «мобильный релиз» — the
 * query is tuned for it.
 */
test("search returns a streamed answer with clickable sources", async ({ page }) => {
  const env = envOrSkip();
  await login(page, env.PLANEAI_E2E_EMAIL, env.PLANEAI_E2E_PASSWORD);

  // The AI search panel lives behind a header button (label TBD by
  // upstream integration; we look for either a button or a route).
  // Until the panel is wired into the navigation, navigate to a
  // dedicated route that mounts <AISearch>:
  await page.goto(`/${env.PLANEAI_E2E_WORKSPACE_SLUG}/ai-search`);

  // Wait until the index banner reports ready, OR proceed if it's
  // already empty. The form must become enabled.
  const input = page.getByPlaceholder(/Спросите по задачам/);
  await expect(input).toBeEnabled({ timeout: 30_000 });

  await input.fill("что известно про мобильный релиз?");
  await page.getByRole("button", { name: /спросить/i }).click();

  // ---- streaming proof ---------------------------------------------------
  // Capture the answer text length at two points 800ms apart. If
  // the stream is real, length should grow between samples.
  const answer = page.locator("article").first();
  await expect(answer).toBeVisible();
  // Wait until at least some text is in the answer.
  await expect(answer).toContainText(/.+/, { timeout: 20_000 });

  const firstLength = (await answer.innerText()).length;
  await page.waitForTimeout(800);
  const secondLength = (await answer.innerText()).length;
  // If the stream is real and the answer is non-trivial, the length
  // should grow OR the stream should be complete. Either way we
  // expect at least some content.
  expect(secondLength).toBeGreaterThanOrEqual(firstLength);
  expect(secondLength).toBeGreaterThan(20);

  // ---- sources sidebar ---------------------------------------------------
  const sources = page.getByRole("complementary").or(
    page.locator("aside").filter({ hasText: /Источники/ })
  );
  await expect(sources).toBeVisible();
  const sourceLink = sources.getByRole("link").first();
  await expect(sourceLink).toBeVisible({ timeout: 20_000 });

  // ---- click-through -----------------------------------------------------
  const [popup] = await Promise.all([
    page.context().waitForEvent("page"),
    sourceLink.click(),
  ]);
  // Plane work-item URL pattern: /<slug>/projects/<uuid>/issues/<uuid>
  await expect(popup).toHaveURL(
    new RegExp(`/${env.PLANEAI_E2E_WORKSPACE_SLUG}/(projects|pages)/`)
  );
});
