import { expect, test } from "@playwright/test";

import { cancelActiveRun, chat, lastReply, login, newChat, openSidebar, send } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

test.afterEach(async ({ page }) => cancelActiveRun(page));

test("regenerate and edit create versions you can switch between", async ({ page }) => {
  const marker = `branch-${Date.now()}`;
  await send(page, marker);
  await expect(lastReply(page)).toContainText(marker);

  await chat(page).getByRole("button", { name: "Regenerate" }).click();
  await expect(chat(page).getByText("2 / 2")).toBeVisible();
  await expect(lastReply(page)).toContainText("hello from the fake model");
  await chat(page).getByRole("button", { name: "Previous version" }).click();
  await expect(chat(page).getByText("1 / 2")).toBeVisible();

  await chat(page).getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Edit message").fill(`${marker} edited`);
  await page.getByRole("button", { name: "Save & send" }).click();
  await expect(lastReply(page)).toContainText(`${marker} edited`);
  // The user message now has two versions; its reply is a fresh branch with one.
  await expect(chat(page).getByText("2 / 2")).toHaveCount(1);

  // Back to the original question: its most recent reply (the regenerated one) is shown.
  await chat(page).getByRole("button", { name: "Previous version" }).click();
  await expect(chat(page).getByTestId("user-message")).toHaveText([marker]);
  await expect(chat(page).getByText("1 / 2")).toBeVisible();
  await expect(chat(page).getByText("2 / 2")).toBeVisible();
});

test("projects group chats and carry their own instructions", async ({ page }) => {
  const name = `Project ${Date.now()}`;
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: "New project" }).click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("Instructions for every chat in this project").fill("Be brief.");
  await dialog.getByRole("button", { name: "Save" }).click();

  await sidebar.getByRole("button", { name: `+ New chat in ${name}` }).click();
  const message = `inside ${name}`;
  await send(page, message);
  await expect(lastReply(page)).toContainText(message);
  await expect(chat(page).locator("header").getByText(name, { exact: true })).toBeVisible();

  await openSidebar(page);
  const chats = page.getByRole("list", { name: "Chats" });
  await expect(chats.getByRole("button", { name: message, exact: true })).toBeVisible();
  await sidebar.getByRole("button", { name: "All chats" }).click();
  await expect(chats.getByRole("button", { name: message, exact: true })).toBeVisible();
});
