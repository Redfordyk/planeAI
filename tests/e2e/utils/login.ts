import type { Page } from "@playwright/test";

/**
 * Sign in via Plane's email+password form. Plane has multiple auth
 * paths (magic link, OAuth) — we use password because it's the
 * only deterministic option in CI without a mail receiver. The
 * staging user is seeded with a known password (see STAGING.md).
 */
export async function login(
  page: Page,
  email: string,
  password: string
): Promise<void> {
  await page.goto("/");

  // If already authenticated, Plane redirects away from /accounts/sign-in.
  if (!page.url().includes("/sign-in") && !page.url().includes("/accounts")) {
    return;
  }

  await page.getByPlaceholder(/email/i).fill(email);
  await page.getByRole("button", { name: /continue|next|продолжить/i }).click();

  await page.getByPlaceholder(/password/i).fill(password);
  await page.getByRole("button", { name: /sign in|log in|войти/i }).click();

  // Wait for the workspace dashboard to render — using the
  // workspace switcher as the readiness signal.
  await page.waitForURL(/\/[^/]+\/(projects|workspaces|home)/, {
    timeout: 30_000,
  });
}
