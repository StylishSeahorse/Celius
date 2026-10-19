import { expect, type Page, test } from "@playwright/test";

const USER = process.env.E2E_USERNAME ?? "e2e";
const PASSWORD = process.env.E2E_PASSWORD ?? "e2e-password-123";

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Message")).toBeVisible();
}

async function newChat(page: Page) {
  const sidebar = page.getByRole("navigation", { name: "Conversations" });
  if (!(await sidebar.isVisible())) await page.getByLabel("Open conversations").click();
  await sidebar.getByRole("button", { name: "+ New chat" }).click();
}

async function send(page: Page, text: string) {
  await page.getByLabel("Message").fill(text);
  await page.getByLabel("Message").press("Enter");
}

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

// A failed test can leave a slow run going, which would hold the single worker slot.
test.afterEach(async ({ page }) => {
  const match = page.url().match(/#\/c\/([0-9a-f-]{36})/);
  if (!match) return;
  const detail = await (await page.request.get(`/api/v1/conversations/${match[1]}`)).json();
  if (detail.active_run) await page.request.post(`/api/v1/runs/${detail.active_run.id}/cancel`);
});

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
