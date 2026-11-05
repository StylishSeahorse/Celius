import { expect, test } from "@playwright/test";

import { login, openSidebar, USER } from "./helpers";

/** Phase 4: an administrator sets a model's prompt profile and capabilities. */
test("set a model's profile and capability overrides", async ({ page }) => {
  await login(page); // the e2e user is the administrator
  // Seen once by the registry (listing models records them).
  expect((await page.request.get("/api/v1/models")).status()).toBe(200);

  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: USER }).click();
  const account = page.getByRole("dialog", { name: "Account" });
  const entry = account.getByTestId("model-entry").first();
  // The fake model reports an 8k context: automatically a lite model.
  await expect(entry).toContainText("lite (auto)");

  await entry.getByLabel(/^Profile for/).selectOption("advanced");
  await expect(entry).toContainText("advanced (admin)");
  await entry.getByLabel(/^Several tool calls per turn for/).selectOption("false");
  await expect(entry.getByLabel(/^Several tool calls per turn for/)).toHaveValue("false");

  const registry = await (await page.request.get("/api/v1/models/registry")).json();
  expect(registry[0].effective.profile).toBe("advanced");
  expect(registry[0].effective.parallel_tools).toBe(false);

  // Back to automatic, so other tests see the default.
  await entry.getByLabel(/^Profile for/).selectOption("");
  await entry.getByLabel(/^Several tool calls per turn for/).selectOption("");
  await expect(entry).toContainText("lite (auto)");
});
