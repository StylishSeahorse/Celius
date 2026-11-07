import { expect, test } from "@playwright/test";

import { lastReply, login, newChat, openSidebar, send, USER } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
});

// Presets change this user's defaults: put them back for the other tests.
test.afterEach(async ({ page }) => {
  await page.request.put("/api/v1/assistant/autonomy", { data: { preset: "balanced" } });
});

test("insights reflect what happened, and autonomy presets apply together", async ({ page }) => {
  await newChat(page);
  await send(page, "Hello for the numbers");
  await expect(lastReply(page)).toContainText("You said");

  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: USER }).click();
  const account = page.getByRole("dialog", { name: "Account" });
  const insights = account.getByTestId("insights");
  await expect(insights).toContainText("Replies");
  await expect(insights).toContainText("completed");
  await account.getByLabel("Whose").selectOption("everyone");
  await expect(insights).toContainText("Replies");

  const autonomy = account.getByRole("region", { name: "How much Celius does on its own" });
  await autonomy.getByRole("button", { name: "Careful" }).click();
  await expect(autonomy.getByTestId("autonomy")).toContainText("Tasks from chats: off");
  await expect(autonomy.getByTestId("autonomy")).toContainText("conservative");
  await autonomy.getByRole("button", { name: "Hands-off" }).click();
  await expect(autonomy.getByTestId("autonomy")).toContainText("kept without asking");
  await expect(autonomy.getByTestId("autonomy")).toContainText("Always asks first: sending email");
  const skills = await (await page.request.get("/api/v1/skills/settings")).json();
  expect(skills.trust).toBe("autonomous");
});
