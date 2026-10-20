import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, type Page, test } from "@playwright/test";

import { cancelActiveRun, chat, lastReply, login, newChat, openSidebar, send } from "./helpers";

// A 2×2 PNG, so the "image" path is exercised without image libraries in the test.
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP4z8DwHwyBNAMDAwMDAwBsAQwC5Jr3dQAAAABJRU5ErkJggg==",
  "base64",
);

function fixture(dir: string, name: string, content: string | Buffer): string {
  const path = join(dir, name);
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, content);
  return path;
}

async function attach(page: Page, paths: string[], expectReady = paths.length) {
  await page.getByLabel("Choose files").setInputFiles(paths);
  await expect(page.locator('[data-testid="attachment-chip"][data-state="ready"]')).toHaveCount(expectReady, {
    timeout: 15_000,
  });
}

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

test.afterEach(async ({ page }) => cancelActiveRun(page));

test("attached files reach the model and download byte-for-byte", async ({ page }, info) => {
  const dir = info.outputPath("files");
  const notes = `Plant tomatoes in May. ${Date.now()}\nCafé ☕ ünïcode\n`;
  const notesPath = fixture(dir, "notes.md", notes);
  const imagePath = fixture(dir, "sky.png", PNG);

  await attach(page, [notesPath, imagePath]);
  await send(page, "What do my notes say?");
  await expect(lastReply(page)).toContainText("I can see these attachments: notes.md, sky.png");
  await expect(lastReply(page)).toContainText("I can see 1 image(s)");

  const attached = chat(page).getByRole("list", { name: "Attached files" });
  await expect(attached.getByRole("img", { name: "sky.png" })).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    attached.getByRole("link", { name: /notes\.md/ }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("notes.md");
  const received = readFileSync((await download.path())!);
  expect(createHash("sha256").update(received).digest("hex")).toBe(
    createHash("sha256").update(notes).digest("hex"),
  );
});

test("a large upload survives a dropped connection", async ({ page }, info) => {
  // 9 MB: three 4 MB chunks. The second chunk's connection fails once; the upload asks the
  // server how much arrived and carries on.
  const line = "Resumable uploads keep going over flaky mobile links.\n";
  const big = line.repeat(Math.ceil((9 * 1024 * 1024) / line.length));
  const path = fixture(info.outputPath("files"), "big-notes.txt", big);
  let chunks = 0;
  let dropped = false;
  await page.route("**/api/v1/files/uploads/*/content", async (route) => {
    chunks += 1;
    if (chunks === 2 && !dropped) {
      dropped = true;
      return route.abort("connectionreset");
    }
    return route.continue();
  });

  await attach(page, [path]);
  expect(dropped).toBe(true);
  expect(chunks).toBe(4); // 3 chunks + the retried one

  await send(page, "Here are my notes.");
  await expect(lastReply(page)).toContainText("big-notes.txt");
  const attached = chat(page).getByRole("list", { name: "Attached files" });
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    attached.getByRole("link", { name: /big-notes\.txt/ }).click(),
  ]);
  const received = readFileSync((await download.path())!);
  expect(received.length).toBe(Buffer.byteLength(big));
  expect(createHash("sha256").update(received).digest("hex")).toBe(createHash("sha256").update(big).digest("hex"));
});

test("a file that isn't what its name says is refused", async ({ page }, info) => {
  const fake = fixture(info.outputPath("files"), "holiday.png", "this is not an image");
  await page.getByLabel("Choose files").setInputFiles([fake]);
  const chip = page.locator('[data-testid="attachment-chip"]');
  await expect(chip).toHaveAttribute("data-state", "error");
  await expect(page.getByRole("alert")).toContainText("not a valid image");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
});

test("a folder keeps its structure", async ({ page }, info) => {
  test.skip(info.project.name === "mobile", "folder picking is a desktop browser feature");
  const dir = info.outputPath("project");
  fixture(dir, "README.md", "# Demo\n");
  fixture(dir, "src/app.py", "print('hi')\n");
  await page.getByLabel("Choose a folder").setInputFiles(dir);
  await expect(page.locator('[data-testid="attachment-chip"][data-state="ready"]')).toHaveCount(2, {
    timeout: 15_000,
  });
  await send(page, "Review this folder");
  await expect(lastReply(page)).toContainText("project/README.md");
  await expect(lastReply(page)).toContainText("project/src/app.py");
});

test("thinking can be switched off and on", async ({ page }) => {
  const toggle = page.getByRole("button", { name: /^Thinking:/ });
  await expect(toggle).toHaveText("Thinking: Auto");
  await toggle.click();
  await toggle.click();
  await expect(toggle).toHaveText("Thinking: Off");
  await send(page, `no thinking ${Date.now()}`);
  await expect(lastReply(page)).toContainText("no thinking");
  await expect(chat(page).getByText("Reasoning")).toHaveCount(0);

  // The setting belongs to the conversation, so it survives a reload.
  await page.reload();
  await expect(page.getByRole("button", { name: /^Thinking:/ })).toHaveText("Thinking: Off");
  await page.getByRole("button", { name: /^Thinking:/ }).click();
  await expect(page.getByRole("button", { name: /^Thinking:/ })).toHaveText("Thinking: Auto");
  await send(page, "now think");
  await expect(lastReply(page)).toContainText("now think");
  await expect(chat(page).getByText("Reasoning")).toHaveCount(1);
});

test("conversations export to Markdown and code blocks download", async ({ page }) => {
  const marker = `export-${Date.now()}`;
  await send(page, marker);
  await expect(lastReply(page)).toContainText(marker);
  // Download only once the whole code block has streamed in.
  await expect(lastReply(page)).toContainText("print('hello from the fake model')");

  const [code] = await Promise.all([
    page.waitForEvent("download"),
    chat(page).getByRole("button", { name: "Download" }).click({ force: true }),
  ]);
  expect(code.suggestedFilename()).toBe("snippet.py");
  expect(readFileSync((await code.path())!, "utf8")).toContain("print('hello from the fake model')");

  await chat(page).getByRole("button", { name: "Export" }).click();
  const [markdown] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("menuitem", { name: "Markdown (.md)" }).click(),
  ]);
  expect(markdown.suggestedFilename()).toBe(`${marker}.md`);
  const text = readFileSync((await markdown.path())!, "utf8");
  expect(text).toContain(`# ${marker}`);
  expect(text).toContain("## Assistant");
});

test("project files are given to every chat in the project", async ({ page }, info) => {
  const name = `Files ${Date.now()}`;
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: "New project" }).click();
  await page.getByRole("dialog", { name: "New project" }).getByLabel("Name").fill(name);
  await page.getByRole("dialog", { name: "New project" }).getByRole("button", { name: "Save" }).click();

  await sidebar.getByRole("button", { name: `Project settings for ${name}` }).click();
  const dialog = page.getByRole("dialog", { name: "Project settings" });
  const guide = fixture(info.outputPath("files"), "style-guide.md", "Always use metric units.\n");
  await dialog.getByLabel("Add project files").setInputFiles([guide]);
  await expect(dialog.getByRole("link", { name: "style-guide.md" })).toBeVisible();
  await expect(dialog.getByText("Reading…")).toHaveCount(0, { timeout: 10_000 });
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await openSidebar(page);
  await sidebar.getByRole("button", { name: `+ New chat in ${name}` }).click();
  await send(page, "How tall is it?");
  await expect(lastReply(page)).toContainText("Project files I know: style-guide.md");
});
