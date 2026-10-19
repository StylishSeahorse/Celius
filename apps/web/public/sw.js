/*
 * Celius service worker: makes the app shell available offline.
 *
 * - API requests (/api/*) are never touched: conversation data lives in IndexedDB and the
 *   server stays authoritative.
 * - Navigations: network first, falling back to the saved shell when offline.
 * - /assets/*: fingerprinted by Vite, so cache-first is always correct.
 */
const SHELL = "celius-shell-v1";
const STATIC = ["/favicon.svg", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

function assetsIn(html) {
  return [...new Set([...html.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]))];
}

async function saveShell(response) {
  const cache = await caches.open(SHELL);
  const html = await response.clone().text();
  await cache.put("/", response);
  const assets = assetsIn(html);
  await Promise.all(
    assets.map(async (url) => {
      if (!(await cache.match(url))) await cache.add(url);
    }),
  );
  // Drop assets from previous builds.
  for (const request of await cache.keys()) {
    const path = new URL(request.url).pathname;
    if (path.startsWith("/assets/") && !assets.includes(path)) await cache.delete(request);
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL);
      await cache.addAll(STATIC);
      await saveShell(await fetch("/", { cache: "no-store" }));
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) if (name !== SHELL) await caches.delete(name);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          const response = await fetch(request);
          if (response.ok) event.waitUntil(saveShell(response.clone()));
          return response;
        } catch {
          return (await caches.match("/")) ?? Response.error();
        }
      })(),
    );
    return;
  }

  event.respondWith(
    (async () => {
      const cached = await caches.match(request);
      if (cached && url.pathname.startsWith("/assets/")) return cached;
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(SHELL);
          event.waitUntil(cache.put(request, response.clone()));
        }
        return response;
      } catch (error) {
        if (cached) return cached;
        throw error;
      }
    })(),
  );
});
