# planeAI E2E (TZ 2.8)

Playwright suite that drives the planeAI search panel through a
running staging instance. Lives outside `apps/web` so it never ships
with Plane's frontend bundle.

## When to run

- **PR pipeline:** not run — depends on a live staging Plane + real
  Anthropic credentials. Keeping it off PRs avoids per-PR token
  cost and staging-state flake.
- **Post-deploy on staging:** the `planeai-deploy-staging.yml`
  workflow (TZ 0.9) can be extended to invoke `pnpm --filter
  planeai-e2e test` once staging is provisioned (TZ 0.10).
- **Locally:** see below.

## Local run

```bash
cd tests/e2e
pnpm install
pnpm run install-browsers   # one-time per machine

# Required env (point at a staging instance with the synthetic seed):
export PLANEAI_E2E_BASE_URL=https://staging.example.com
export PLANEAI_E2E_EMAIL=qa@example.com
export PLANEAI_E2E_PASSWORD='...'
export PLANEAI_E2E_WORKSPACE_SLUG=planeai-staging

pnpm test
```

If any env var is missing, all tests skip themselves (see
`utils/env.ts`). That is intentional — silently passing would hide
the fact that the staging contract wasn't checked.

## Test inventory

| Spec | Scenario |
|---|---|
| `specs/search-happy-path.spec.ts` | full flow: login → open panel → ask → verify streaming (sample answer length at two points 800ms apart) → click source link → land on correct work item |
| `specs/search-negative.spec.ts` | index-not-ready (mocked) disables input + shows progress; 429 budget exceeded surfaces with budget copy |

## Flakiness discipline (TZ 2.8 §Важно)

- No `page.waitForTimeout()` longer than the 800 ms used to *sample*
  streaming growth — never used as a "wait until done" hack.
- Streaming completeness is asserted with `expect(answer)
  .toContainText(/.+/)` + a length comparison, not a fixed sleep.
- Index banner state is intercepted via Playwright `route` for the
  negative tests so they don't depend on the staging index being in
  a specific coverage range.

## Future work

- Real-streaming negative case: simulate provider 5xx mid-stream
  and assert the `{error: ...}` SSE frame shows up in the UI as an
  error banner rather than a half-finished answer.
- Per-project ACL E2E: log in as a guest user who only has access
  to project A, ask a question whose synthetic answer lives in
  project B, assert the answer says «данных недостаточно».
