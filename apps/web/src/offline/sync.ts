/**
 * Follows the server's change feed so edits from other devices show up here.
 *
 * Polls every few seconds while the page is visible and online (cheap: one indexed query
 * when nothing changed), and immediately on reconnect or when the tab regains focus.
 */
import { api, isNetworkError } from "../lib/api";
import { newId } from "../lib/ids";
import type { SyncChange } from "../lib/types";
import { cache } from "./db";

const POLL_MS = 4000;

/**
 * `onChanges(changes, resync)`: with `resync` the caller should reload everything (first run
 * on this device, or the server's history no longer matches ours, e.g. after a restore).
 */
export function startSync(
  onChanges: (changes: SyncChange[], resync: boolean) => void,
): () => void {
  let stopped = false;
  let validated = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let busy = false;

  async function deviceId(): Promise<string> {
    let id = await cache.get<string>("device_id");
    if (!id) {
      id = newId();
      await cache.set("device_id", id);
    }
    return id;
  }

  async function tick() {
    if (stopped || busy) return;
    if (document.visibilityState !== "visible" || !navigator.onLine) return schedule();
    busy = true;
    try {
      let cursor = await cache.get<number>("sync_cursor");
      if (!validated) {
        // Once per session: take the server's cursor *before* reloading, so nothing that
        // changes during the reload is missed.
        const server = (await api.syncCursor()).cursor;
        validated = true;
        if (cursor === undefined || cursor > server) {
          await cache.set("sync_cursor", server);
          onChanges([], true);
          return;
        }
      }
      cursor ??= 0;
      const all: SyncChange[] = [];
      for (;;) {
        const page = await api.syncChanges(cursor);
        all.push(...page.changes);
        cursor = page.cursor;
        if (!page.has_more) break;
      }
      if (all.length) {
        onChanges(all, false);
        await cache.set("sync_cursor", cursor);
        void api.syncAck(await deviceId(), cursor).catch(() => undefined);
      }
    } catch (error) {
      if (!isNetworkError(error)) console.warn("sync failed", error);
    } finally {
      busy = false;
      schedule();
    }
  }

  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(tick, POLL_MS);
  }

  const now = () => {
    clearTimeout(timer);
    void tick();
  };
  window.addEventListener("online", now);
  document.addEventListener("visibilitychange", now);
  void tick();

  return () => {
    stopped = true;
    clearTimeout(timer);
    window.removeEventListener("online", now);
    document.removeEventListener("visibilitychange", now);
  };
}
