import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { refreshPush } from "./lib/notifications";
import "./styles/index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Offline app shell. Service workers need a secure context (HTTPS or localhost); over
// plain HTTP the app still works, just not offline from a cold start.
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .then(() => refreshPush())
      .catch((error) => console.warn("SW registration failed", error));
  });
  // A notification was clicked while the app was already open: go to its conversation.
  navigator.serviceWorker.addEventListener("message", (event) => {
    const data = event.data as { type?: string; url?: string } | null;
    if (data?.type === "celius:open" && data.url && new URL(data.url).origin === location.origin) {
      location.href = data.url;
    }
    // A push arrived: say which chat is on screen, so it isn't notified about.
    if (data?.type === "celius:showing?") {
      event.ports[0]?.postMessage({
        conversation: location.hash.match(/^#\/c\/([0-9a-f-]{36})$/)?.[1] ?? null,
        visible: document.visibilityState === "visible",
      });
    }
  });
}
