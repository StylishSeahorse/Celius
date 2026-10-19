import { expect, test } from "@playwright/test";

import {
  cancelActiveRun,
  chat,
  conversationIdOf,
  getConversation,
  lastReply,
  login,
  newChat,
  openSidebar,
  secondDevice,
  send,
} from "./helpers";

const userTexts = (detail: { messages: { role: string; parts: { text: string | null }[] }[] }) =>
  detail.messages.filter((m) => m.role === "user").map((m) => m.parts.map((p) => p.text).join(""));

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

test.afterEach(async ({ page }) => cancelActiveRun(page));

test("messages written offline are queued and delivered exactly once", async ({ page, context }) => {
  const marker = `offline-${Date.now()}`;
  await context.setOffline(true);
  await send(page, `${marker} one`);
  await send(page, `${marker} two`);
  const pending = chat(page).getByTestId("pending-message");
  await expect(pending).toHaveCount(2);
  await expect(pending.first()).toContainText("Queued");
  await expect(page.getByRole("status")).toContainText("You're offline");

  await context.setOffline(false);
  // The first is answered, then the second goes out and is answered too.
  await expect(lastReply(page)).toContainText(`${marker} two`, { timeout: 30_000 });
  await expect(pending).toHaveCount(0);

  const detail = await getConversation(page.request, conversationIdOf(page)!);
  expect(userTexts(detail)).toEqual([`${marker} one`, `${marker} two`]);
});

test("a lost response is retried without duplicating the message", async ({ page }) => {
  let dropped = false;
  await page.route("**/api/v1/conversations/*/messages", async (route) => {
    if (dropped) return route.continue();
    dropped = true;
    await route.fetch(); // the server receives and stores the message…
    await route.abort("connectionreset"); // …but the response never arrives
  });
  const marker = `lost-${Date.now()}`;
  await send(page, marker);
  await expect(lastReply(page)).toContainText(marker, { timeout: 30_000 });

  const detail = await getConversation(page.request, conversationIdOf(page)!);
  expect(userTexts(detail)).toEqual([marker]);
  await expect(chat(page).getByTestId("user-message")).toHaveText([marker]);
});

test("another device sees new messages and renames without reloading", async ({ page, browser, baseURL }) => {
  const marker = `sync-${Date.now()}`;
  await send(page, `${marker} first`);
  await expect(lastReply(page)).toContainText(`${marker} first`);
  const id = conversationIdOf(page)!;

  const other = await secondDevice(browser, baseURL);
  await other.page.goto(`/#/c/${id}`);
  await expect(lastReply(other.page)).toContainText(`${marker} first`);

  await send(page, `${marker} second`);
  await expect(lastReply(other.page)).toContainText(`${marker} second`, { timeout: 20_000 });

  await chat(page).getByRole("heading").click();
  await page.getByLabel("Conversation title").fill(`Renamed ${marker}`);
  await page.getByLabel("Conversation title").press("Enter");
  await expect(chat(other.page).getByRole("heading", { name: `Renamed ${marker}` })).toBeVisible({
    timeout: 20_000,
  });
  const sidebar = await openSidebar(other.page);
  await expect(sidebar.getByRole("button", { name: `Renamed ${marker}` })).toBeVisible();
  await other.context.close();
});

test("conflicting renames ask which version to keep", async ({ page, browser, baseURL }) => {
  const marker = `conflict-${Date.now()}`;
  await send(page, marker);
  await expect(lastReply(page)).toContainText(marker);
  const id = conversationIdOf(page)!;

  const other = await secondDevice(browser, baseURL);
  // Keep the second device's view stale so its rename is based on an old version.
  await other.page.route("**/api/v1/sync/**", (route) => route.abort());
  await other.page.goto(`/#/c/${id}`);
  await expect(lastReply(other.page)).toContainText(marker);

  const rename = async (p: typeof page, title: string) => {
    await chat(p).getByRole("heading").click();
    await p.getByLabel("Conversation title").fill(title);
    await p.getByLabel("Conversation title").press("Enter");
  };
  await rename(page, "Theirs title");
  await expect(chat(page).getByRole("heading", { name: "Theirs title" })).toBeVisible();

  await rename(other.page, "Mine title");
  const dialog = other.page.getByRole("dialog", { name: "Changed on another device" });
  await expect(dialog).toContainText("Theirs: Theirs title");
  await expect(dialog).toContainText("Yours: Mine title");
  await dialog.getByRole("button", { name: "Use mine" }).click();
  await expect(dialog).toBeHidden();
  expect((await getConversation(page.request, id)).title).toBe("Mine title");

  // And the other choice: the second device (still not syncing) is stale again.
  await page.reload();
  await expect(chat(page).getByRole("heading", { name: "Mine title" })).toBeVisible();
  await rename(page, "Newer title");
  await expect(chat(page).getByRole("heading", { name: "Newer title" })).toBeVisible();
  await rename(other.page, "Discarded title");
  await other.page.getByRole("button", { name: "Keep theirs" }).click();
  await expect(chat(other.page).getByRole("heading", { name: "Newer title" })).toBeVisible();
  expect((await getConversation(page.request, id)).title).toBe("Newer title");
  await other.context.close();
});
