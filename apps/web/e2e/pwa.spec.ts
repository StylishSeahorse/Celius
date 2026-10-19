import { expect, test } from "@playwright/test";

import {
  chat,
  conversationIdOf,
  lastReply,
  login,
  newChat,
  send,
  waitForSavedConversation,
} from "./helpers";

test("installable manifest and icons are served", async ({ request }) => {
  const manifest = await request.get("/manifest.webmanifest");
  expect(manifest.headers()["content-type"]).toContain("application/manifest+json");
  const body = await manifest.json();
  expect(body.display).toBe("standalone");
  for (const icon of body.icons) {
    const response = await request.get(icon.src);
    expect(response.headers()["content-type"]).toBe("image/png");
  }
  const sw = await request.get("/sw.js");
  expect(sw.headers()["cache-control"]).toContain("no-store");
});

test("the app opens offline with saved conversations", async ({ page, context }) => {
  await login(page);
  await newChat(page);
  const marker = `pwa-${Date.now()}`;
  await send(page, marker);
  await expect(lastReply(page)).toContainText(marker);
  const id = conversationIdOf(page)!;

  await page.evaluate(() => navigator.serviceWorker.ready.then(() => undefined));
  await page.reload(); // now controlled by the service worker
  await expect(lastReply(page)).toContainText(marker);

  await waitForSavedConversation(page, id);
  await context.setOffline(true);
  await page.goto(`/#/c/${id}`);
  await page.reload();
  await expect(chat(page).getByTestId("user-message")).toHaveText([marker]);
  await expect(lastReply(page)).toContainText(marker);
  await expect(page.getByRole("status")).toContainText("You're offline");
  await context.setOffline(false);
});
