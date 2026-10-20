import { expect, type Page, test } from "@playwright/test";

import { chat, conversationIdOf, lastReply, login, newChat, openSidebar, send } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

async function memories(page: Page, status: "active" | "proposed") {
  const response = await page.request.get(`/api/v1/memories?status=${status}`);
  return (await response.json()) as { id: string; content: string }[];
}

async function openMemory(page: Page) {
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /^Memory/ }).click();
  return page.getByRole("dialog", { name: "Memory" });
}

async function waitForProposal(page: Page, content: string) {
  await expect
    .poll(async () => (await memories(page, "proposed")).map((m) => m.content), { timeout: 15_000 })
    .toContain(content);
}

test("a suggested memory waits for review, then is used and shown", async ({ page }) => {
  const fact = `The user likes green tea ${Date.now()}`;
  await send(page, `[remember:${fact}] hello`);
  await expect(lastReply(page)).toContainText("You said");
  await waitForProposal(page, fact);

  const panel = await openMemory(page);
  await panel.getByRole("button", { name: /To review/ }).click();
  const item = panel.getByTestId("memory").filter({ hasText: fact });
  await item.getByRole("button", { name: "Remember", exact: true }).click();
  await expect(item).toHaveCount(0);
  await panel.getByRole("button", { name: "Remembered" }).click();
  await expect(panel.getByTestId("memory").filter({ hasText: fact })).toBeVisible();
  await panel.getByRole("button", { name: "Close" }).click();

  await newChat(page);
  await send(page, "Which tea do I like?");
  const used = chat(page).getByTestId("memory-used").last();
  await expect(used).toContainText("Used");
  await used.locator("summary").click();
  await expect(used).toContainText(fact);

  // Clean up so other tests start from a known state.
  const created = (await memories(page, "active")).find((m) => m.content === fact)!;
  await page.request.delete(`/api/v1/memories/${created.id}`);
});

test("an incognito chat is not remembered", async ({ page }) => {
  await page.getByLabel("Memory for this chat").click(); // a new chat: no confirmation
  await expect(page.getByLabel("Memory for this chat")).toHaveAttribute("aria-pressed", "false");
  const fact = `The user has a secret ${Date.now()}`;
  await send(page, `[remember:${fact}] keep this between us`);
  await expect(lastReply(page)).toContainText("You said");
  const id = conversationIdOf(page)!;
  const info = await (await page.request.get(`/api/v1/conversations/${id}/memory`)).json();
  expect(info).toEqual({ memory_active: false, excerpts: 0, memories_learned: 0 });
  expect((await memories(page, "proposed")).map((m) => m.content)).not.toContain(fact);
});

test("deleting a chat can forget what was learned from it", async ({ page }) => {
  const fact = `The user plays chess ${Date.now()}`;
  await send(page, `[remember:${fact}] chess talk ${Date.now()}`);
  await waitForProposal(page, fact);
  const id = conversationIdOf(page)!;

  const sidebar = await openSidebar(page);
  const row = sidebar.getByRole("list", { name: "Chats" }).locator("li").filter({ hasText: "chess talk" }).first();
  await row.hover();
  await row.getByRole("button", { name: /^Delete/ }).click();
  const dialog = page.getByRole("dialog", { name: /Delete/ });
  await dialog.getByLabel(/Also forget the 1 memory/).check();
  await dialog.getByRole("button", { name: "Delete" }).click();

  await expect.poll(async () => (await page.request.get(`/api/v1/conversations/${id}`)).status()).toBe(404);
  expect((await memories(page, "proposed")).map((m) => m.content)).not.toContain(fact);
});

test("add, edit and delete a memory by hand", async ({ page }) => {
  const panel = await openMemory(page);
  await panel.getByRole("button", { name: "Remembered" }).click();
  const fact = `I prefer metric units ${Date.now()}`;
  await panel.getByLabel("New memory").fill(fact);
  await panel.getByRole("button", { name: "Add" }).click();
  const item = panel.getByTestId("memory").filter({ hasText: fact });
  await expect(item).toBeVisible();
  await item.getByRole("button", { name: "Edit" }).click();
  await item.getByLabel("Memory text").fill(`${fact} and 24h time`);
  await item.getByRole("button", { name: "Save" }).click();
  const edited = panel.getByTestId("memory").filter({ hasText: "24h time" });
  await expect(edited).toBeVisible();
  page.once("dialog", (d) => void d.accept());
  await edited.getByRole("button", { name: "Delete" }).click();
  await expect(edited).toHaveCount(0);
});
