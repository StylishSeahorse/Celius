import { expect, test } from "@playwright/test";

import { chat, lastReply, login, newChat, send } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

test("a task's plan shows under the chat, pauses when a reply fails, and resumes", async ({ page }) => {
  await send(page, "[tool:plan] tidy up please");
  await expect(lastReply(page)).toContainText("Tool result");
  const card = page.getByTestId("task-card");
  await expect(card).toContainText("Tidy the workspace");
  await expect(card).toContainText("In progress");
  await expect(card.getByLabel("0 of 3 steps done")).toBeVisible();

  await card.getByRole("button", { name: /Tidy the workspace/ }).click();
  await expect(card.getByTestId("task-step")).toHaveCount(3);

  await send(page, "[fail]");
  await expect(card).toContainText("Paused");
  await expect(card).toContainText("The reply failed");

  await card.getByRole("button", { name: "Resume" }).click();
  await expect(chat(page)).toContainText("Continue the task: Tidy the workspace");
  await expect(card).toContainText("In progress");

  page.once("dialog", (d) => void d.accept());
  await card.getByRole("button", { name: "Stop task" }).click();
  await expect(card).toHaveCount(0);
});
