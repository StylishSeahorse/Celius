import { expect, type Page, test } from "@playwright/test";

import { login, newChat, openSidebar } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

async function openToday(page: Page) {
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /^Today/ }).click();
  return page.getByRole("dialog", { name: "Today" });
}

function local(offsetDays: number, hhmm: string) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${hhmm}`;
}

test("calendar: add events and see overlaps", async ({ page }) => {
  const zone = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  await page.request.put("/api/v1/assistant/settings", { data: { timezone: zone } });
  const stamp = Date.now().toString(36);
  const panel = await openToday(page);
  await panel.getByRole("button", { name: "Calendar" }).click();
  for (const [title, start, end] of [
    [`Standup ${stamp}`, "09:00", "09:30"],
    [`Dentist ${stamp}`, "09:15", "10:00"],
  ]) {
    await panel.getByLabel("Event title").fill(title);
    await panel.getByLabel("Starts").fill(local(1, start));
    await panel.getByLabel("Ends").fill(local(1, end));
    await panel.getByRole("button", { name: "Add", exact: true }).click();
    await expect(panel.getByTestId("calendar-event").filter({ hasText: title })).toBeVisible();
  }
  await expect(panel.getByTestId("calendar-event").filter({ hasText: `Dentist ${stamp}` })).toContainText("overlaps");
  page.once("dialog", (d) => void d.accept());
  await panel.getByRole("button", { name: `Delete Standup ${stamp}` }).click();
  await expect(panel.getByTestId("calendar-event").filter({ hasText: `Standup ${stamp}` })).toHaveCount(0);
});

test("automations: create from a recipe, it fires, history shows it", async ({ page }) => {
  const stamp = Date.now().toString(36);
  await page.request.post("/api/v1/tasks", { data: { title: `Late ${stamp}`, due: local(-3, "00:00").slice(0, 10) } });
  const panel = await openToday(page);
  await panel.getByRole("button", { name: "Automations" }).click();
  await panel.getByLabel("Start from").selectOption("overdue");
  const definition = JSON.parse(await panel.getByLabel("Definition").inputValue());
  definition.name = `Overdue ${stamp}`;
  await panel.getByLabel("Definition").fill(JSON.stringify(definition));
  await panel.getByRole("button", { name: "Create" }).click();
  const rule = panel.getByTestId("automation").filter({ hasText: `Overdue ${stamp}` });
  await expect(rule).toContainText("When a task becomes overdue");
  // The worker checks task rules every few seconds in the e2e stack.
  await expect
    .poll(
      async () => {
        const rules = (await (await page.request.get("/api/v1/automations")).json()) as { name: string; last_status: string | null }[];
        return rules.find((r) => r.name === `Overdue ${stamp}`)?.last_status ?? null;
      },
      { timeout: 20_000 },
    )
    .toBe("ok");
  await rule.getByRole("button", { name: "History" }).click();
  await expect(rule).toContainText(`task_overdue: Late ${stamp}`);
  await rule.getByLabel(`On: Overdue ${stamp}`).uncheck();
  await expect(rule.getByLabel(`On: Overdue ${stamp}`)).not.toBeChecked();
});

test("connect a calendar account", async ({ page }) => {
  const name = `Cloud ${Date.now().toString(36)}`;
  const panel = await openToday(page);
  await panel.getByRole("button", { name: "Settings" }).click();
  await panel.getByLabel("Account name").fill(name);
  await panel.getByLabel("Calendar URL").fill("https://cal.example.invalid/dav/");
  await panel.getByLabel("User name").fill("robin");
  await panel.getByLabel("Password").fill("secret");
  await panel.getByRole("button", { name: "Connect" }).click();
  const row = panel.getByTestId("account").filter({ hasText: name });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Check" }).click();
  await expect(row).toContainText(/can't reach|resolve|Could not/i);
  const accounts = (await (await page.request.get("/api/v1/integrations/accounts")).json()) as { name: string; has_password: boolean; config: object }[];
  const mine = accounts.find((a) => a.name === name)!;
  expect(mine.has_password).toBe(true);
  expect(JSON.stringify(mine)).not.toContain("secret");
  page.once("dialog", (d) => void d.accept());
  await row.getByRole("button", { name: `Remove ${name}` }).click();
  await expect(row).toHaveCount(0);
});
