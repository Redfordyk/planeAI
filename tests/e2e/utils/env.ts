import { test as base } from "@playwright/test";

/**
 * Helper: gather required env vars in one place and skip the whole
 * test if any are missing. Saves a `skipIf` line on every test.
 */
export const requiredEnv = [
  "PLANEAI_E2E_BASE_URL",
  "PLANEAI_E2E_EMAIL",
  "PLANEAI_E2E_PASSWORD",
  "PLANEAI_E2E_WORKSPACE_SLUG",
] as const;

export function envOrSkip(): Record<(typeof requiredEnv)[number], string> {
  const missing = requiredEnv.filter((k) => !process.env[k]);
  if (missing.length) {
    base.skip(true, `Missing env vars: ${missing.join(", ")}`);
  }
  return Object.fromEntries(
    requiredEnv.map((k) => [k, process.env[k]!])
  ) as Record<(typeof requiredEnv)[number], string>;
}
