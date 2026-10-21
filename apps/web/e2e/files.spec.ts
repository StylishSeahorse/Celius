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

// One page, "Preview me" (the same generator as the API's tests).
const PDF = Buffer.from(
  "JVBERi0xLjQKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCAzMDAgMTQ0XSAvQ29udGVudHMgNCAwIFIgL1Jlc291cmNlcyA8PCAvRm9udCA8PCAvRjEgNSAwIFIgPj4gPj4gPj4KZW5kb2JqCjQgMCBvYmoKPDwgL0xlbmd0aCA0MSA+PgpzdHJlYW0KQlQgL0YxIDE4IFRmIDIwIDEwMCBUZCAoUHJldmlldyBtZSkgVGogRVQKZW5kc3RyZWFtCmVuZG9iago1IDAgb2JqCjw8IC9UeXBlIC9Gb250IC9TdWJ0eXBlIC9UeXBlMSAvQmFzZUZvbnQgL0hlbHZldGljYSA+PgplbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTggMDAwMDAgbiAKMDAwMDAwMDExNSAwMDAwMCBuIAowMDAwMDAwMjQxIDAwMDAwIG4gCjAwMDAwMDAzMzIgMDAwMDAgbiAKdHJhaWxlcgo8PCAvU2l6ZSA2IC9Sb290IDEgMCBSID4+CnN0YXJ0eHJlZgo0MDIKJSVFT0YK",
  "base64",
);

/** One second of a 440 Hz tone: 8 kHz, 16-bit mono PCM in a WAV container. */
function wav(seconds = 1, rate = 8000): Buffer {
  const samples = seconds * rate;
  const out = Buffer.alloc(44 + samples * 2);
  out.write("RIFF", 0);
  out.writeUInt32LE(36 + samples * 2, 4);
  out.write("WAVEfmt ", 8);
  out.writeUInt32LE(16, 16); // fmt chunk size
  out.writeUInt16LE(1, 20); // PCM
  out.writeUInt16LE(1, 22); // mono
  out.writeUInt32LE(rate, 24);
  out.writeUInt32LE(rate * 2, 28); // bytes per second
  out.writeUInt16LE(2, 32); // block align
  out.writeUInt16LE(16, 34); // bits per sample
  out.write("data", 36);
  out.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) out.writeInt16LE(Math.round(8000 * Math.sin((2 * Math.PI * 440 * i) / rate)), 44 + i * 2);
  return out;
}

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

test("a PDF shows a preview of its first page", async ({ page }, info) => {
  const path = fixture(info.outputPath("files"), "flyer.pdf", PDF);
  await attach(page, [path]);
  await send(page, "What does the flyer say?");
  await expect(lastReply(page)).toContainText("flyer.pdf");
  const preview = chat(page).getByTestId("file-preview");
  await expect(preview).toHaveAttribute("href", /\/download\?inline=1$/);
  const image = preview.getByRole("img", { name: "Preview of flyer.pdf" });
  await expect(image).toBeVisible();
  // The thumbnail really loaded (not a broken image).
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(512);
});

test("an audio file plays in the chat and can be sought", async ({ page }, info) => {
  const path = fixture(info.outputPath("files"), "tone.wav", wav());
  await attach(page, [path]);
  await send(page, "What is this recording?");
  await expect(lastReply(page)).toContainText("tone.wav");
  const player = chat(page).getByTestId("media-attachment");
  await expect(player).toContainText("tone.wav");
  const audio = player.locator("audio");
  await expect(audio).toHaveAttribute("src", /\/download\?inline=1$/);
  // The browser really loaded it: its length is known, and jumping ahead works (range requests).
  await expect.poll(() => audio.evaluate((a: HTMLAudioElement) => Math.round(a.duration * 10) / 10)).toBe(1);
  const position = await audio.evaluate(
    (a: HTMLAudioElement) =>
      new Promise<number>((resolve) => {
        a.addEventListener("seeked", () => resolve(a.currentTime), { once: true });
        a.currentTime = 0.5;
      }),
  );
  expect(position).toBeCloseTo(0.5, 1);
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
