import { expect, test } from "@playwright/test";

import { login, openSidebar, USER } from "./helpers";

/** Phase 10: invite someone, share a project with them, then switch their account off. */
test("invite, join, share a project, disable", async ({ page, browser, baseURL }) => {
  const name = `fam${Date.now().toString(36)}`;
  await login(page); // the e2e user is the administrator

  // Invite.
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: USER }).click();
  const account = page.getByRole("dialog", { name: "Account" });
  await account.getByRole("button", { name: "Invite someone" }).click();
  const link = await account.getByLabel("Invitation link").inputValue();
  expect(link).toMatch(/#\/join\/[A-Za-z0-9_-]{20,}$/);
  await account.getByRole("button", { name: "Close" }).click();

  // They join on their own device.
  const theirs = await browser.newContext({ baseURL });
  const them = await theirs.newPage();
  await them.goto(link.replace(/^https?:\/\/[^/]+/, ""));
  const join = them.getByRole("form", { name: "Create your account" });
  await join.getByLabel("Username").fill(name);
  await join.getByLabel("Your name (optional)").fill("Family Member");
  await join.getByLabel("Password (12+ characters)").fill("family password 123");
  await join.getByLabel("Password again").fill("family password 123");
  await join.getByRole("button", { name: "Create account" }).click();
  await expect(them.getByLabel("Message")).toBeVisible();

  // The link can't be used twice.
  const stranger = await browser.newContext({ baseURL });
  const again = await stranger.newPage();
  await again.goto(link.replace(/^https?:\/\/[^/]+/, ""));
  await expect(again.getByRole("alert")).toContainText("expired or was already used");
  await stranger.close();

  // Share a project with them, read-only.
  const project = await (
    await page.request.post("/api/v1/projects", { data: { name: `Holidays ${name}`, instructions: "Trains only." } })
  ).json();
  await page.reload();
  const projects = await openSidebar(page);
  await projects.getByRole("button", { name: `Project settings for Holidays ${name}` }).click();
  const dialog = page.getByRole("dialog", { name: "Project settings" });
  await dialog.getByLabel("Username to share with").fill(name);
  await dialog.getByRole("button", { name: "Share" }).click();
  await expect(dialog.getByTestId("member")).toContainText("Family Member");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await them.reload();
  const theirSidebar = await openSidebar(them);
  await expect(theirSidebar.getByRole("button", { name: new RegExp(`Holidays ${name}`) }).first()).toContainText(USER);
  await theirSidebar.getByRole("button", { name: `Project settings for Holidays ${name}` }).click();
  const shared = them.getByRole("dialog", { name: "Project settings" });
  await expect(shared.getByTestId("shared-with-me")).toContainText(USER);
  await expect(shared.getByLabel("Instructions for every chat in this project")).toBeDisabled();
  await expect(shared.getByRole("button", { name: "Save" })).toHaveCount(0);
  await shared.getByRole("button", { name: "Cancel" }).click();

  // Switch their account off: they're signed out.
  await page.getByRole("navigation", { name: "Conversations" }).getByRole("button", { name: USER }).click();
  const people = page.getByRole("dialog", { name: "Account" });
  const person = people.locator(`[data-testid="person"][data-username="${name}"]`);
  await person.getByRole("button", { name: "Disable" }).click();
  await expect(person).toContainText("disabled");
  await them.reload();
  await expect(them.getByLabel("Username")).toBeVisible();

  await theirs.close();
  expect(project.id).toBeTruthy();
});
