import { type APIRequestContext, type Browser, expect, type Page } from "@playwright/test";

export const USER = process.env.E2E_USERNAME ?? "e2e";
export const PASSWORD = process.env.E2E_PASSWORD ?? "e2e-password-123";

export async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Message")).toBeVisible();
}

export async function openSidebar(page: Page) {
  const sidebar = page.getByRole("navigation", { name: "Conversations" });
  if (!(await sidebar.isVisible())) await page.getByLabel("Open conversations").click();
  return sidebar;
}

export async function newChat(page: Page) {
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /^\+ New chat/ }).click();
}

export async function send(page: Page, text: string) {
  await page.getByLabel("Message").fill(text);
  await page.getByLabel("Message").press("Enter");
}

export function chat(page: Page) {
  return page.getByRole("main", { name: "Chat" });
}

export function lastReply(page: Page) {
  return page.locator(".markdown").last();
}

export function conversationIdOf(page: Page): string | null {
  return page.url().match(/#\/c\/([0-9a-f-]{36})/)?.[1] ?? null;
}

/** A second, independent device (its own cookies, storage and IndexedDB). */
export async function secondDevice(browser: Browser, baseURL: string | undefined) {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await login(page);
  return { context, page };
}

export async function getConversation(request: APIRequestContext, id: string) {
  const response = await request.get(`/api/v1/conversations/${id}`);
  expect(response.ok()).toBeTruthy();
  return response.json();
}

/** Cancel any reply left running, so a failed test doesn't hold the single worker slot. */
export async function cancelActiveRun(page: Page) {
  const id = conversationIdOf(page);
  if (!id) return;
  const response = await page.request.get(`/api/v1/conversations/${id}`);
  if (!response.ok()) return;
  const detail = await response.json();
  if (detail.active_run) await page.request.post(`/api/v1/runs/${detail.active_run.id}/cancel`);
}

/** Wait until this device has saved the conversation with a finished last reply. */
export async function waitForSavedConversation(page: Page, id: string) {
  await expect
    .poll(() =>
      page.evaluate(
        (conversationId) =>
          new Promise<string | null>((resolve) => {
            const open = indexedDB.open("celius");
            open.onerror = () => resolve(null);
            open.onsuccess = () => {
              const get = open.result.transaction("details").objectStore("details").get(conversationId);
              get.onsuccess = () => resolve(get.result?.messages?.at(-1)?.status ?? null);
              get.onerror = () => resolve(null);
            };
          }),
        id,
      ),
    )
    .toBe("complete");
}
