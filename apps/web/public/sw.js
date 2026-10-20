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

/*
 * Notifications (Web Push). The server sends {title, body, url, tag}; tag is the
 * conversation id. Nothing is shown while that conversation is on screen.
 */
/** Ask each open window what it shows (hash routes aren't reliably in Client.url). */
function askWindow(client) {
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => resolve(null), 500);
    channel.port1.onmessage = (event) => {
      clearTimeout(timer);
      resolve(event.data);
    };
    client.postMessage({ type: "celius:showing?" }, [channel.port2]);
  });
}

async function someoneIsShowing(conversationId) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const answers = await Promise.all(windows.map(askWindow));
  return answers.some((a) => a && a.visible && a.conversation === conversationId);
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    (async () => {
      if (data.tag && data.tag !== "test" && (await someoneIsShowing(data.tag))) return;
      await self.registration.showNotification(data.title || "Celius", {
        body: data.body || "",
        tag: data.tag || undefined,
        data: { url: data.url || "/" },
        icon: "/icon-192.png",
        badge: "/icon-192.png",
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
      if (open) {
        open.postMessage({ type: "celius:open", url });
        return open.focus();
      }
      return self.clients.openWindow(url);
    })(),
  );
});
