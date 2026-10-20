import { expect, test } from "@playwright/test";

import { lastReply, login, newChat, send } from "./helpers";

/**
 * The desktop app's web side, with a stand-in for its native bridge that records calls:
 * a reply that finishes while the window is in the background raises a native
 * notification; one that finishes in front of you doesn't.
 */
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as unknown as {
      __TAURI_INTERNALS__: unknown;
      __calls: { cmd: string; args?: Record<string, unknown> }[];
      __focused: boolean;
    };
    w.__calls = [];
    w.__focused = true;
    w.__TAURI_INTERNALS__ = {
      invoke: async (cmd: string, args?: Record<string, unknown>) => {
        w.__calls.push({ cmd, args });
        return cmd === "list_roots" ? [] : null;
      },
    };
    document.hasFocus = () => w.__focused;
  });
  await login(page);
  await newChat(page);
});

function notifications(page: import("@playwright/test").Page) {
  return page.evaluate(() =>
    (window as unknown as { __calls: { cmd: string; args?: { title: string; body: string } }[] }).__calls
      .filter((c) => c.cmd === "notify")
      .map((c) => c.args),
  );
}

test("a reply that finishes in the background notifies natively", async ({ page }) => {
  await page.evaluate(() => ((window as unknown as { __focused: boolean }).__focused = false));
  await send(page, "Tell me about tomatoes");
  await expect(lastReply(page)).toContainText("Tell me about tomatoes");
  await expect.poll(() => notifications(page)).toHaveLength(1);
  const [note] = await notifications(page);
  expect(note!.body).toContain("Tell me about tomatoes");
});

test("no notification for a reply you are watching", async ({ page }) => {
  await send(page, "Hello there");
  await expect(lastReply(page)).toContainText("Hello there");
  await page.waitForTimeout(500);
  expect(await notifications(page)).toHaveLength(0);
});
