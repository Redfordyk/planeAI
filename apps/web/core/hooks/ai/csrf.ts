/**
 * Read the Django CSRF cookie and produce the headers needed for an
 * authenticated POST. Plane's API uses DRF `SessionAuthentication`,
 * which enforces CSRF on every unsafe method (POST/PUT/PATCH/DELETE);
 * fetch() with `credentials: "same-origin"` ships the session cookie
 * but does NOT auto-attach the `X-CSRFToken` header — we have to.
 *
 * If the cookie is absent (first visit, before any GET that sets
 * it), we hit `/auth/get-csrf-token/` once to make the server mint
 * one. The result is cached for the lifetime of the page.
 */

let cached: string | null = null;
let inflight: Promise<string | null> | null = null;

function readCookie(name: string): string | null {
  const m = document.cookie.match(
    new RegExp("(?:^|; )" + name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1") + "=([^;]*)")
  );
  return m ? decodeURIComponent(m[1]) : null;
}

async function fetchToken(): Promise<string | null> {
  if (cached) return cached;
  if (inflight) return inflight;
  // First try the cookie — it's set on any prior GET to the API.
  const fromCookie = readCookie("csrftoken");
  if (fromCookie) {
    cached = fromCookie;
    return cached;
  }
  inflight = (async () => {
    try {
      const r = await fetch("/auth/get-csrf-token/", {
        credentials: "same-origin",
      });
      if (!r.ok) return null;
      const body = (await r.json()) as { csrf_token?: string };
      // Plane returns the value in JSON *and* sets the cookie. We
      // honour both — JSON is authoritative for this hit; the cookie
      // serves later page loads.
      const v = body.csrf_token || readCookie("csrftoken");
      cached = v ?? null;
      return cached;
    } catch {
      return null;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

/**
 * Returns headers with X-CSRFToken populated (and Content-Type if
 * given). Pass the result to `fetch()` along with
 * `credentials: "same-origin"`.
 */
export async function csrfHeaders(
  extra: Record<string, string> = {}
): Promise<Record<string, string>> {
  const token = await fetchToken();
  const out: Record<string, string> = { ...extra };
  if (token) out["X-CSRFToken"] = token;
  return out;
}
