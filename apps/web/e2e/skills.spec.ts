import { expect, type Page, test } from "@playwright/test";

import { login, newChat, openSidebar } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

async function openSkills(page: Page) {
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /^Skills/ }).click();
  return page.getByRole("dialog", { name: "Skills" });
}

test("write a skill, publish a new version and roll back", async ({ page }) => {
  const name = `check-disk-${Date.now()}`;
  const panel = await openSkills(page);
  await panel.getByRole("button", { name: "Skills", exact: true }).click();
  await panel.getByRole("button", { name: "Write a skill" }).click();
  await panel
    .getByLabel("SKILL.md")
    .fill(`---\nname: ${name}\ndescription: Check free disk space on a server and report it.\n---\n\n1. Run df -h.\n`);
  await panel.getByRole("button", { name: "Save" }).click();

  await expect(panel.getByRole("heading", { name })).toBeVisible();
  await expect(panel.getByTestId("skill-file")).toContainText("Run df -h");
  await panel.getByRole("button", { name: "Edit" }).click();
  await panel.getByLabel("Edit SKILL.md").fill(
    `---\nname: ${name}\ndescription: Check free disk space on a server and report it.\n---\n\n1. Run df -h.\n2. Report the fullest mount.\n`,
  );
  await panel.getByRole("button", { name: "Save as new version" }).click();
  await expect(panel.getByTestId("skill-file")).toContainText("fullest mount");
  await panel.getByRole("button", { name: "Roll back to this" }).click();
  await expect(panel.getByTestId("skill-file")).not.toContainText("fullest mount");

  await panel.getByRole("button", { name: "Back to skills" }).click();
  await panel.getByLabel("Search skills").fill("disk space");
  await expect(panel.getByTestId("skill").filter({ hasText: name })).toBeVisible();
});

test("skill trust settings", async ({ page }) => {
  const panel = await openSkills(page);
  await panel.getByRole("button", { name: "Settings" }).click();
  await panel.getByLabel(/^Conservative/).click();
  await expect(panel.getByLabel(/^Conservative/)).toBeChecked();
  await expect(panel.getByLabel(/Learn skills from finished tasks/)).toBeDisabled();
  await panel.getByLabel(/^Balanced/).click();
  await expect(panel.getByLabel(/^Balanced/)).toBeChecked();
  await expect(panel.getByLabel(/Learn skills from finished tasks/)).toBeEnabled();
});

test("a memory that contradicts another is resolved in Conflicts", async ({ page }) => {
  // Letters only: digits would make memories of other runs "differ in numbers" too.
  const stamp = String(Date.now()).replace(/\d/g, (d) => "abcdefghij"[Number(d)]);
  for (const city of ["Oslo", "Bergen"]) {
    const response = await page.request.post("/api/v1/memories", {
      data: { content: `The user's office ${stamp} is located in ${city}.` },
    });
    expect(response.status()).toBe(201);
  }
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: /^Memory/ }).click();
  const panel = page.getByRole("dialog", { name: "Memory" });
  await panel.getByRole("button", { name: /^Conflicts/ }).click();
  const conflict = panel.getByTestId("conflict").filter({ hasText: String(stamp) });
  await expect(conflict).toBeVisible();
  await conflict.getByRole("button", { name: "Keep the newer" }).click();
  await expect(conflict).toHaveCount(0);
  await panel.getByRole("button", { name: "Remembered" }).click();
  await panel.getByLabel("Search memories").fill(String(stamp));
  await expect(panel.getByTestId("memory")).toHaveCount(1);
  await expect(panel.getByTestId("memory")).toContainText("Bergen");
});
