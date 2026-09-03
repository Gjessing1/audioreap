const CACHE = "audioreap-shell-v5";
const SHELL = [
  "/",
  "/static/app.css",
  "/static/app.js",
  "/static/native.js",
  "/static/htmx.min.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Static assets: stale-while-revalidate. The cached copy answers immediately, and
  // the network copy replaces it for next time. Cache-first without the revalidation
  // pinned the UI to whatever CSS/JS was cached on first visit until this file's CACHE
  // name happened to be bumped by hand — invisible in a browser tab that reloads
  // often, but the Android app IS this web UI, so a deploy has to reach it.
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(e.request).then((cached) => {
          const fresh = fetch(e.request).then((response) => {
            if (response && response.ok) cache.put(e.request, response.clone());
            return response;
          });
          if (!cached) return fresh;
          // Answer from cache and refresh behind it. waitUntil keeps the worker alive
          // for the refresh, and swallows its failure — an unreachable server is the
          // normal offline case, not an unhandled rejection.
          e.waitUntil(fresh.catch(() => {}));
          return cached;
        })
      )
    );
    return;
  }
  // App shell: serve cached index for navigation requests when offline
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(() => caches.match("/"))
    );
    return;
  }
  // Everything else: network only
});
