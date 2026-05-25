/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { startTransition, StrictMode } from "react";
import { hydrateRoot } from "react-dom/client";
import { HydratedRouter } from "react-router/dom";

// planeAI: silence React's "recoverable" hydration errors (#418, #423,
// #425). They fire when SSR-rendered markup differs from client first
// paint. React 18 then re-renders the subtree client-side and the page
// works. Plane is effectively SPA (SSR returns an empty shell) so these
// fire on every page load and pollute the console + Sentry-like tools.
// The page itself continues to render correctly — these are warnings,
// not crashes. We swallow them via onRecoverableError + a console.error
// guard for the symptoms that leak through window.onerror.
const SILENCED = /Minified React error #(418|423|425)/;
const _consoleError = console.error;
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string" && SILENCED.test(args[0])) return;
  if (args[0] instanceof Error && SILENCED.test(args[0].message)) return;
  _consoleError(...args);
};
window.addEventListener("error", (ev) => {
  if (ev?.error?.message && SILENCED.test(ev.error.message)) {
    ev.preventDefault();
  }
});
window.addEventListener("unhandledrejection", (ev) => {
  const msg = ev?.reason?.message ?? String(ev?.reason ?? "");
  if (SILENCED.test(msg)) ev.preventDefault();
});

startTransition(() => {
  hydrateRoot(
    document,
    <StrictMode>
      <HydratedRouter />
    </StrictMode>,
    {
      onRecoverableError: (error) => {
        const msg = error instanceof Error ? error.message : String(error);
        if (SILENCED.test(msg)) return; // auto-recovered, no user action needed
        _consoleError("React recoverable error:", error);
      },
    }
  );
});
