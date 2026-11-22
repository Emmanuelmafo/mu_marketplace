const CACHE_NAME = "mu-marketplace-static-v6";
const STATIC_ASSETS = [
  "/static/css/style.css",
  "/static/js/pull-to-refresh.js",
  "/static/js/script.js",
  "/static/images/branding/mu-marketplace-logo.png",
  "/static/images/branding/search-icon.png",
  "/static/images/categories/all.webp",
  "/static/images/categories/accessories.webp",
  "/static/images/categories/beauty.webp",
  "/static/images/categories/books.webp",
  "/static/images/categories/electronics.webp",
  "/static/images/categories/fashion.webp",
  "/static/images/categories/groceries.webp",
  "/static/images/categories/home.webp",
  "/static/images/categories/sports.webp",
  "/static/images/pwa/icon-192.png",
  "/static/images/pwa/icon-512.png",
  "/static/images/pwa/icon-512-maskable.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

// Do not cache HTML pages or account/order data. Requests always reach Flask,
// so marketplace closure and role permissions cannot be bypassed by stale pages.
self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (request.mode === "navigate" || !url.pathname.startsWith("/static/")) return;

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    })
  );
});
