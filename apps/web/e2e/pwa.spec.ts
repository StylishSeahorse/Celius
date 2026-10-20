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
  expect(body.share_target).toMatchObject({ action: "/share-target", method: "POST" });
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

test("something shared from another app opens a new chat with it attached", async ({ page }) => {
  await login(page);
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  // What Android's share sheet does: a multipart POST to the manifest's share_target.
  await page.evaluate(() => {
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/share-target";
    form.enctype = "multipart/form-data";
    const text = document.createElement("input");
    text.name = "text";
    text.value = "Look at this recipe";
    const url = document.createElement("input");
    url.name = "url";
    url.value = "https://example.com/soup";
    const files = document.createElement("input");
    files.type = "file";
    files.name = "files";
    const transfer = new DataTransfer();
    transfer.items.add(new File(["2 onions\n1 litre stock\n"], "soup.txt", { type: "text/plain" }));
    files.files = transfer.files;
    form.append(text, url, files);
    document.body.append(form);
    form.submit();
  });

  await expect(page.getByLabel("Message")).toHaveValue("Look at this recipe\nhttps://example.com/soup");
  await expect(page.locator('[data-testid="attachment-chip"][data-state="ready"]')).toHaveCount(1, {
    timeout: 15_000,
  });
  expect(page.url()).not.toContain("share=");
  await send(page, "Summarise it");
  await expect(lastReply(page)).toContainText("soup.txt");
  expect(conversationIdOf(page)).not.toBeNull();

  // The shared copy is gone from the device once used.
  const left = await page.evaluate(async () => (await (await caches.open("celius-share")).keys()).length);
  expect(left).toBe(0);
  await expect(chat(page)).toBeVisible();
});
