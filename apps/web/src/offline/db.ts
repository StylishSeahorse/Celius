/**
 * Local cache and outbox in IndexedDB. The server stays authoritative: everything here is
 * either a copy of server state (to render instantly and offline) or a pending write.
 *
 * Every function degrades to a no-op when IndexedDB is unavailable (some private modes),
 * so the app still works online without a cache.
 */
import { type DBSchema, type IDBPDatabase, openDB } from "idb";

import type { ConversationDetail, ConversationSummary, Project } from "../lib/types";

export interface OutboxItem {
  /** Also the server's idempotency key: retries can never create a duplicate. */
  client_message_id: string;
  conversation_id: string;
  content: string;
  created_at: number;
  /** Set when the conversation was started offline and doesn't exist on the server yet. */
  create_conversation?: { model_override: string | null; project_id: string | null };
  status: "queued" | "failed";
  error?: string;
}

interface CeliusDB extends DBSchema {
  kv: { key: string; value: unknown };
  conversations: { key: string; value: ConversationSummary };
  details: { key: string; value: ConversationDetail };
  projects: { key: string; value: Project };
  outbox: { key: string; value: OutboxItem; indexes: { by_created: number } };
}

let dbPromise: Promise<IDBPDatabase<CeliusDB> | null> | null = null;

function db(): Promise<IDBPDatabase<CeliusDB> | null> {
  dbPromise ??= openDB<CeliusDB>("celius", 1, {
    upgrade(database) {
      database.createObjectStore("kv");
      database.createObjectStore("conversations", { keyPath: "id" });
      database.createObjectStore("details", { keyPath: "id" });
      database.createObjectStore("projects", { keyPath: "id" });
      database
        .createObjectStore("outbox", { keyPath: "client_message_id" })
        .createIndex("by_created", "created_at");
    },
  }).catch(() => null);
  return dbPromise;
}

async function safe<T>(fallback: T, fn: (d: IDBPDatabase<CeliusDB>) => Promise<T>): Promise<T> {
  try {
    const d = await db();
    return d ? await fn(d) : fallback;
  } catch {
    return fallback;
  }
}

export const cache = {
  get: <T>(key: string) => safe<T | undefined>(undefined, (d) => d.get("kv", key) as Promise<T>),
  set: (key: string, value: unknown) => safe(undefined, async (d) => void (await d.put("kv", value, key))),

  conversations: () => safe<ConversationSummary[]>([], (d) => d.getAll("conversations")),
  putConversations: (items: ConversationSummary[], replaceAll: boolean) =>
    safe(undefined, async (d) => {
      const tx = d.transaction("conversations", "readwrite");
      if (replaceAll) await tx.store.clear();
      await Promise.all(items.map((item) => tx.store.put(item)));
      await tx.done;
    }),
  removeConversation: (id: string) =>
    safe(undefined, async (d) => {
      await d.delete("conversations", id);
      await d.delete("details", id);
    }),

  detail: (id: string) => safe<ConversationDetail | undefined>(undefined, (d) => d.get("details", id)),
  putDetail: (detail: ConversationDetail) =>
    safe(undefined, async (d) => void (await d.put("details", detail))),

  projects: () => safe<Project[]>([], (d) => d.getAll("projects")),
  putProjects: (items: Project[]) =>
    safe(undefined, async (d) => {
      const tx = d.transaction("projects", "readwrite");
      await tx.store.clear();
      await Promise.all(items.map((item) => tx.store.put(item)));
      await tx.done;
    }),

  outbox: () => safe<OutboxItem[]>([], (d) => d.getAllFromIndex("outbox", "by_created")),
  putOutbox: (item: OutboxItem) => safe(undefined, async (d) => void (await d.put("outbox", item))),
  removeOutbox: (id: string) => safe(undefined, (d) => d.delete("outbox", id)),

  /** Sign-out: nothing from this account may remain on the device. */
  clearAll: () =>
    safe(undefined, async (d) => {
      const stores = ["kv", "conversations", "details", "projects", "outbox"] as const;
      const tx = d.transaction(stores, "readwrite");
      await Promise.all(stores.map((s) => tx.objectStore(s).clear()));
      await tx.done;
    }),
};
