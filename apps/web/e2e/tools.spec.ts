import { expect, test } from "@playwright/test";

import { cancelActiveRun, chat, lastReply, login, newChat, openSidebar, send } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

test.afterEach(async ({ page }) => {
  await cancelActiveRun(page);
});

test("a write asks for approval, then runs exactly once", async ({ page }) => {
  await page.getByLabel("Tool permissions").selectOption("standard");
  await send(page, "[tool:write] save a greeting");

  const card = chat(page).getByTestId("tool-call");
  await expect(card).toHaveAttribute("data-status", "pending_approval");
  await expect(card).toContainText("Hello from Celius"); // the exact arguments are shown
  await expect(chat(page).getByText("Waiting for your approval above.")).toBeVisible();
  const sidebar = await openSidebar(page);
  await expect(sidebar.getByLabel(/waiting for approval/)).toBeVisible();
  const viewport = page.viewportSize();
  // Phone layout: close the sidebar by tapping the dimmed area beside it.
  if (viewport && viewport.width < 768) await page.mouse.click(viewport.width - 10, viewport.height / 2);

  await card.getByRole("button", { name: "Allow once" }).click();
  await expect(lastReply(page)).toContainText("Wrote 17 bytes to hello.txt");
  await expect(card).toHaveAttribute("data-status", "completed");
  await expect(card.getByRole("button", { name: "Allow once" })).toHaveCount(0);
});

test("python runs in the sandbox under the developer profile", async ({ page }) => {
  await page.getByLabel("Tool permissions").selectOption("developer");
  await send(page, "[tool:python] compute");
  const card = chat(page).getByTestId("tool-call");
  await expect(card).toHaveAttribute("data-status", "completed");
  await expect(lastReply(page)).toContainText("42");
  // The steps are folded away under the answer until asked for.
  const work = chat(page).getByTestId("work-panel").last();
  await expect(work).toContainText("Used 1 tool");
  await expect(card).toBeHidden();
  await work.locator("summary").click();
  await expect(card).toBeVisible();
  await card.getByRole("button").first().click(); // expand
  await expect(card).toContainText("print(6 * 7)");
});

test("read only refuses writes without asking", async ({ page }) => {
  await page.getByLabel("Tool permissions").selectOption("read_only");
  await send(page, "[tool:list] what is there");
  await expect(chat(page).getByTestId("tool-call")).toHaveAttribute("data-status", "completed");
  await expect(lastReply(page)).toContainText("Tool result:");
});

test("approvals inbox: deny from the tools panel", async ({ page }) => {
  await page.getByLabel("Tool permissions").selectOption("standard");
  await send(page, "[tool:write] another");
  await expect(chat(page).getByTestId("tool-call")).toHaveAttribute("data-status", "pending_approval");

  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /Tools & activity/ }).click();
  const panel = page.getByRole("dialog", { name: "Tools" });
  await expect(panel.getByTestId("tool-call")).toBeVisible();
  await panel.getByRole("button", { name: "Deny" }).click();
  await expect(panel.getByText("Nothing is waiting for your approval.")).toBeVisible();

  await panel.getByRole("button", { name: "Activity" }).click();
  await expect(panel.getByTestId("activity-item").first()).toBeVisible();
  await panel.getByRole("button", { name: "Permissions" }).click();
  await expect(panel.getByText(/Running \(/)).toBeVisible(); // the sandbox is up
  await panel.getByRole("button", { name: "Close" }).click();

  await expect(lastReply(page)).toContainText("denied");
});

test("create a scheduled task", async ({ page }) => {
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /Tools & activity/ }).click();
  const panel = page.getByRole("dialog", { name: "Tools" });
  await panel.getByRole("button", { name: "Scheduled" }).click();
  await panel.getByRole("button", { name: "New scheduled task" }).click();
  const name = `Digest ${Date.now()}`;
  await panel.getByLabel("Task name").fill(name);
  await panel.getByLabel("Prompt").fill("Summarise my day");
  await panel.getByRole("button", { name: "Create" }).click();
  const row = panel.getByTestId("schedule").filter({ hasText: name });
  await expect(row).toContainText("Daily at 08:00");
  page.once("dialog", (d) => void d.accept());
  await row.getByRole("button", { name: "Delete" }).click();
  await expect(row).toHaveCount(0);
});

test("allow all for this chat stops asking until you say otherwise", async ({ page }) => {
  await page.getByLabel("Tool permissions").selectOption("standard");
  await send(page, "[tool:write] first");
  const first = chat(page).getByTestId("tool-call").first();
  await expect(first).toHaveAttribute("data-status", "pending_approval");
  await first.getByRole("button", { name: "Allow all for this chat" }).click();
  await expect(first).toHaveAttribute("data-status", "completed");
  await expect(chat(page).getByText("Tools run without asking in this chat.")).toBeVisible();

  await send(page, "[tool:write] second");
  const second = chat(page).getByTestId("tool-call").nth(1);
  await expect(second).toHaveAttribute("data-status", "completed"); // no question this time
  await expect(chat(page).getByText("Waiting for your approval above.")).toHaveCount(0);

  await chat(page).getByRole("button", { name: "Ask again" }).click();
  await expect(chat(page).getByText("Tools run without asking in this chat.")).toHaveCount(0);
});

test("the default profile is named in the picker", async ({ page }) => {
  await expect(page.getByLabel("Tool permissions").locator("option").first()).toHaveText(
    /Tools: default \(.+\)/,
  );
});
