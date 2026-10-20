import { api } from "./api";

/** Web Push on this device: "tell me when a reply is ready" (S-10). */

export type PushState = "unsupported" | "denied" | "off" | "on";

export function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = (base64url + "=".repeat((4 - (base64url.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function sameKey(subscription: PushSubscription, key: Uint8Array): boolean {
  const current = subscription.options.applicationServerKey;
  if (!current) return true; // the browser doesn't tell: assume it matches
  const bytes = new Uint8Array(current);
  return bytes.length === key.length && bytes.every((b, i) => b === key[i]);
}

async function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!pushSupported()) return null;
  return (await navigator.serviceWorker.getRegistration()) ?? null;
}

export async function pushState(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  const reg = await registration();
  const subscription = await reg?.pushManager.getSubscription();
  if (!subscription || Notification.permission !== "granted") return "off";
  const info = await api.pushInfo(subscription.endpoint);
  return info.subscribed ? "on" : "off";
}

export async function enablePush(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "denied" : "off";
  const reg = (await registration()) ?? (await navigator.serviceWorker.ready);
  const { public_key } = await api.pushInfo();
  const key = keyBytes(public_key);
  let subscription = await reg.pushManager.getSubscription();
  if (subscription && !sameKey(subscription, key)) {
    await subscription.unsubscribe(); // the server's key changed (new APP_SECRET_KEY)
    subscription = null;
  }
  subscription ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  await api.pushSubscribe(subscription.toJSON());
  return "on";
}

export async function disablePush(): Promise<PushState> {
  const subscription = await (await registration())?.pushManager.getSubscription();
  if (subscription) {
    await api.pushUnsubscribe(subscription.endpoint).catch(() => undefined);
    await subscription.unsubscribe();
  }
  return "off";
}

/** On start: keep the server's copy of this device's subscription current (it is dropped
 * there if the push service says the device is gone, or after a key change). */
export async function refreshPush(): Promise<void> {
  if (!pushSupported() || Notification.permission !== "granted") return;
  const subscription = await (await registration())?.pushManager.getSubscription();
  if (!subscription) return;
  const info = await api.pushInfo(subscription.endpoint);
  if (!info.subscribed) await enablePush();
}
