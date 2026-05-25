// planeAI: workbox-based SW was caching stale React Router manifests
// after deploys, blocking new routes (ai-orchestrator) from being
// recognised client-side. This noop SW unregisters itself and clears
// all caches on first activation; users get a fresh fetch from
// origin on every reload from then on.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.registration.unregister();
    const clients = await self.clients.matchAll({ type: "window" });
    clients.forEach((c) => c.navigate(c.url));
  })());
});
