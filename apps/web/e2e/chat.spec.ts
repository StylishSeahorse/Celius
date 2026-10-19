import { expect, test } from "@playwright/test";

import { cancelActiveRun, login, newChat, send } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

test.afterEach(async ({ page }) => cancelActiveRun(page));

test("streams a markdown reply and lists the conversation", async ({ page }) => {
  const marker = `pelicans-${Date.now()}`;
  await send(page, `Tell me about ${marker}`);
  const chat = page.getByRole("main", { name: "Chat" });
  await expect(chat.getByRole("heading", { name: `Tell me about ${marker}` })).toBeVisible();
  await expect(chat.getByText(`Tell me about ${marker}`).last()).toBeVisible();
  const reply = page.locator(".markdown").last();
  await expect(reply.locator("strong")).toHaveText(`Tell me about ${marker}`);
  await expect(reply.locator("li")).toHaveCount(3);
  await expect(reply.locator("pre code")).toContainText("hello from the fake model");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();

  await send(page, "And a follow-up");
  await expect(page.locator(".markdown").last()).toContainText("This is turn 2.");

  await page.reload();
  await expect(page.locator(".markdown")).toHaveCount(2);
  await expect(page.locator("header h1")).toHaveText(`Tell me about ${marker}`);
});

test("resumes an in-progress reply after a page reload", async ({ page }) => {
  await send(page, "[slow] keep going");
  const reply = page.locator(".markdown").last();
  await expect(reply).toContainText("You said:", { timeout: 10_000 });
  await page.reload();
  // The reply continues from the persisted events and completes.
  await expect(page.locator(".markdown").last()).toContainText("hello from the fake model", {
    timeout: 30_000,
  });
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();
});

test("stop cancels generation and keeps the partial reply", async ({ page }) => {
  await send(page, "[slow] please stop me");
  await expect(page.locator(".markdown").last()).toContainText("You said:", { timeout: 10_000 });
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByText("Stopped")).toBeVisible();
  await expect(page.locator(".markdown").last()).not.toContainText("hello from the fake model");
  await send(page, "after stopping");
  await expect(page.locator(".markdown").last()).toContainText("after stopping");
});

test("a model error is shown and the next message still works", async ({ page }) => {
  await send(page, "[fail] this one breaks");
  await expect(page.getByRole("alert")).toContainText("fake failure requested");
  await send(page, "this one works");
  await expect(page.locator(".markdown").last()).toContainText("this one works");
});
