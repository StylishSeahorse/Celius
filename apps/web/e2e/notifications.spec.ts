import { generateKeyPairSync, randomBytes } from "node:crypto";

import { expect, type Page, test } from "@playwright/test";

import { conversationIdOf, lastReply, login, newChat, openSidebar, send, USER } from "./helpers";

/**
 * Headless Chromium has no push service, so `pushManager.subscribe` is replaced by one
 * returning a real-looking subscription (a valid P-256 key on an FCM endpoint). Pushes are
 * then delivered to the actual service worker through the DevTools protocol, the way the
 * push service would.
 */
function fakeSubscription() {
  const { publicKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const jwk = publicKey.export({ format: "jwk" });
  const raw = Buffer.concat([
    Buffer.from([4]),
    Buffer.from(jwk.x!, "base64url"),
    Buffer.from(jwk.y!, "base64url"),
  ]);
  return {
    endpoint: `https://fcm.googleapis.com/fcm/send/e2e-${randomBytes(8).toString("hex")}`,
    keys: { p256dh: raw.toString("base64url"), auth: randomBytes(16).toString("base64url") },
  };
}

// The full browser (new headless mode): the stripped-down headless shell always reports
// notification permission as denied and shows no notifications.
test.use({ channel: "chromium", permissions: ["notifications"] });

test.beforeEach(async ({ page }) => {
  const subscription = fakeSubscription();
  await page.addInitScript((sub) => {
    let current: unknown = null;
    const fake = {
      endpoint: sub.endpoint,
      options: { applicationServerKey: null },
      toJSON: () => sub,
      unsubscribe: async () => {
        current = null;
        return true;
      },
    };
    PushManager.prototype.subscribe = async function () {
      current = fake;
      return fake as unknown as PushSubscription;
    };
    PushManager.prototype.getSubscription = async function () {
      return current as PushSubscription | null;
    };
  }, subscription);
  await login(page);
  await page.evaluate(() => navigator.serviceWorker.ready.then(() => true));
});

async function openAccount(page: Page) {
  const sidebar = await openSidebar(page);
  await sidebar.getByRole("button", { name: USER }).click();
  return page.getByRole("dialog", { name: "Account" });
}

/**
 * Deliver a push to the real service worker through DevTools, and wait until it says it
 * got it (it tells open windows). DevTools occasionally drops a delivery, so send again.
 */
async function deliverPush(page: Page, data: { tag: string } & Record<string, string>) {
  const cdp = await page.context().newCDPSession(page);
  const registration = new Promise<string>((resolve) => {
    cdp.on("ServiceWorker.workerRegistrationUpdated", ({ registrations }) => {
      const found = registrations.find((r: { isDeleted: boolean }) => !r.isDeleted);
      if (found) resolve(found.registrationId);
    });
  });
  await cdp.send("ServiceWorker.enable");
  const registrationId = await registration;
  const origin = new URL(page.url()).origin;
  for (let attempt = 0; attempt < 5; attempt++) {
    const received = page.evaluate(
      (tag) =>
        new Promise<boolean>((resolve) => {
          const timer = setTimeout(() => resolve(false), 2000);
          navigator.serviceWorker.addEventListener("message", function onMessage(event) {
            if (event.data?.type === "celius:push" && event.data.tag === tag) {
              clearTimeout(timer);
              navigator.serviceWorker.removeEventListener("message", onMessage);
              resolve(true);
            }
          });
        }),
      data.tag,
    );
    await cdp.send("ServiceWorker.deliverPushMessage", { origin, registrationId, data: JSON.stringify(data) });
    if (await received) {
      await cdp.detach();
      return;
    }
  }
  throw new Error("the service worker never received the push");
}

function shownNotifications(page: Page) {
  return page.evaluate(async () =>
    (await (await navigator.serviceWorker.ready).getNotifications()).map((n) => ({
      title: n.title,
      body: n.body,
      tag: n.tag,
    })),
  );
}

test("turning notifications on registers this device with the server", async ({ page }) => {
  const account = await openAccount(page);
  const state = account.getByTestId("push-state");
  await expect(state).toHaveAttribute("data-state", "off");
  await account.getByRole("button", { name: "Turn on for this device" }).click();
  await expect(state).toHaveAttribute("data-state", "on");

  const endpoint = await page.evaluate(
    async () => (await (await navigator.serviceWorker.ready).pushManager.getSubscription())!.endpoint,
  );
  const info = await (await page.request.get(`/api/v1/push?endpoint=${encodeURIComponent(endpoint)}`)).json();
  expect(info.subscribed).toBe(true);

  await account.getByRole("button", { name: "Turn off on this device" }).click();
  await expect(state).toHaveAttribute("data-state", "off");
  const after = await (await page.request.get(`/api/v1/push?endpoint=${encodeURIComponent(endpoint)}`)).json();
  expect(after.subscribed).toBe(false);
});

test("a push shows a notification, except for the chat on screen", async ({ page }) => {
  await newChat(page);
  await send(page, "Hello");
  await expect(lastReply(page)).toContainText("Hello");
  const open = conversationIdOf(page)!;

  // A reply in the conversation being looked at: nothing to notify.
  await deliverPush(page, { title: "This chat", body: "Already visible", tag: open, url: `/#/c/${open}` });
  // One in another conversation: shown.
  const other = "00000000-0000-4000-8000-000000000001";
  await deliverPush(page, { title: "Garden", body: "Tomatoes in May", tag: other, url: `/#/c/${other}` });

  await expect.poll(() => shownNotifications(page)).toEqual([{ title: "Garden", body: "Tomatoes in May", tag: other }]);
});
