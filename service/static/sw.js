const CACHE = "audioreap-shell-v4";
const SHELL = [
  "/",
  "/static/app.css",
  "/static/app.js",
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
  // Only cache static assets — never cache API or UI dynamic responses
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(e.request).then((cached) => cached || fetch(e.request))
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
