import { expect, type Page, test } from "@playwright/test";

import { lastReply, login, newChat, openSidebar, send } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

async function openToday(page: Page) {
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /^Today/ }).click();
  return page.getByRole("dialog", { name: "Today" });
}

function isoDay(offset: number) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0, 10);
}

test("tasks: add, see in the briefing, complete", async ({ page }) => {
  const stamp = Date.now().toString(36);
  // Use this device's time zone, so "today" matches the dates below.
  const zone = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  await page.request.put("/api/v1/assistant/settings", { data: { timezone: zone } });
  await page.request.post("/api/v1/tasks", { data: { title: `Overdue ${stamp}`, due: isoDay(-2) } });

  const panel = await openToday(page);
  const overdue = panel.getByRole("region", { name: "Overdue" });
  await expect(overdue).toContainText(`Overdue ${stamp}`);

  await panel.getByRole("button", { name: "Tasks" }).click();
  await panel.getByLabel("New task").fill(`Buy milk ${stamp}`);
  await panel.getByLabel("Priority").selectOption("high");
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  const row = panel.getByTestId("user-task").filter({ hasText: `Buy milk ${stamp}` });
  await expect(row).toContainText("high");
  await row.getByLabel(`Done: Buy milk ${stamp}`).click();
  await expect(row).toHaveCount(0);
  await panel.getByRole("button", { name: "Done", exact: true }).click();
  await expect(panel.getByTestId("user-task").filter({ hasText: `Buy milk ${stamp}` })).toBeVisible();
});

test("a to-do mentioned in a chat is suggested and can be accepted", async ({ page }) => {
  const title = `Renew passport ${Date.now().toString(36)}`;
  await send(page, `[todo:${title}] I really need to sort this out`);
  await expect(lastReply(page)).toContainText("You said");
  await expect
    .poll(
      async () =>
        ((await (await page.request.get("/api/v1/tasks?status=suggested")).json()) as { title: string }[]).map(
          (t) => t.title,
        ),
      { timeout: 15_000 },
    )
    .toContain(title);

  const panel = await openToday(page);
  const suggestion = panel.getByTestId("suggestion").filter({ hasText: title });
  await suggestion.getByRole("button", { name: "Add to my tasks" }).click();
  await expect(suggestion).toHaveCount(0);
  await panel.getByRole("button", { name: "Tasks" }).click();
  await expect(panel.getByTestId("user-task").filter({ hasText: title })).toBeVisible();
});
