import { expect, test } from "@playwright/test";

import { cancelActiveRun, chat, lastReply, login, newChat, send } from "./helpers";

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

test("steering: a message sent while the reply streams is read and the reply goes on", async ({ page }) => {
  await send(page, "[slow] tell me about the solar system in many many words please");
  const composer = page.getByLabel("Message");
  const stop = page.getByRole("button", { name: "Stop", exact: true }).last();
  await expect(stop).toBeVisible();
  // Mid-answer: the reply has started streaming.
  await expect(lastReply(page)).toContainText("You said", { timeout: 15_000 });
  await composer.fill("Keep it short");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByTestId("queued-steer")).toContainText("Keep it short");

  const steer = chat(page).getByTestId("steer");
  await expect(steer).toContainText("Keep it short", { timeout: 30_000 });
  await expect(page.getByTestId("queued-steer")).toHaveCount(0);
  // The reply continues after it, answering the new message.
  // The first answer stays above it, and the reply continues below it.
  await expect(chat(page).getByText("tell me about the solar system").last()).toBeVisible();
  await expect(lastReply(page)).toContainText("Keep it short", { timeout: 30_000 });
  await expect(lastReply(page)).toContainText("This is turn", { timeout: 30_000 });
  await expect(stop).toHaveCount(0, { timeout: 30_000 });
});
