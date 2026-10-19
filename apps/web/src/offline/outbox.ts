/**
 * Ordered queue of messages waiting to reach the server.
 *
 * Every send goes through here, online or not: the item is persisted first, then flushed.
 * Exactly-once delivery comes from the server, which deduplicates on `client_message_id`;
 * the queue only has to keep retrying until it gets an answer.
 */
import { ApiError, api, isNetworkError } from "../lib/api";
import type { SubmitResponse } from "../lib/types";
import { cache, type OutboxItem } from "./db";

export type { OutboxItem };

export type OutboxEvent =
  | { type: "changed" }
  | { type: "sent"; item: OutboxItem; response: SubmitResponse };

type Listener = (event: OutboxEvent) => void;

/** Errors that mean "try again later" rather than "this message can't be sent". */
function isTransient(error: unknown): boolean {
  if (isNetworkError(error)) return true;
  return error instanceof ApiError && (error.status >= 500 || error.status === 401 || error.status === 429);
}

export class Outbox {
  private items: OutboxItem[] = [];
  private listeners = new Set<Listener>();
  private running: Promise<void> | null = null;
  private again = false;
  private loaded: Promise<void>;

  constructor(private readonly client = api) {
    this.loaded = cache.outbox().then((items) => {
      this.items = items;
      this.emit({ type: "changed" });
    });
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  list(conversationId?: string): OutboxItem[] {
    return this.items.filter((i) => !conversationId || i.conversation_id === conversationId);
  }

  get size(): number {
    return this.items.length;
  }

  async enqueue(item: Omit<OutboxItem, "created_at" | "status">): Promise<void> {
    await this.loaded;
    const full: OutboxItem = { ...item, created_at: Date.now(), status: "queued" };
    this.items.push(full);
    await cache.putOutbox(full);
    this.emit({ type: "changed" });
    void this.flush();
  }

  async discard(clientMessageId: string): Promise<void> {
    this.items = this.items.filter((i) => i.client_message_id !== clientMessageId);
    await cache.removeOutbox(clientMessageId);
    this.emit({ type: "changed" });
  }

  async retry(clientMessageId: string): Promise<void> {
    const item = this.items.find((i) => i.client_message_id === clientMessageId);
    if (!item) return;
    item.status = "queued";
    delete item.error;
    await cache.putOutbox(item);
    this.emit({ type: "changed" });
    await this.flush();
  }

  /** Try to deliver everything. Concurrent calls coalesce into one extra pass. */
  flush(): Promise<void> {
    if (this.running) {
      this.again = true;
      return this.running;
    }
    this.running = (async () => {
      try {
        do {
          this.again = false;
          await this.pass();
        } while (this.again);
      } finally {
        this.running = null;
      }
    })();
    return this.running;
  }

  private async pass(): Promise<void> {
    await this.loaded;
    // Conversations that must wait: a reply is still generating, or an earlier message
    // failed. Later messages in the same conversation never overtake earlier ones.
    const blocked = new Set<string>();
    for (const item of [...this.items].sort((a, b) => a.created_at - b.created_at)) {
      if (blocked.has(item.conversation_id) || item.status === "failed") {
        blocked.add(item.conversation_id);
        continue;
      }
      try {
        if (item.create_conversation) {
          await this.client.createConversation({ id: item.conversation_id, ...item.create_conversation });
          delete item.create_conversation;
          await cache.putOutbox(item);
        }
        const response = await this.client.sendMessage(item.conversation_id, {
          client_message_id: item.client_message_id,
          content: item.content,
          attachment_ids: item.attachments?.map((a) => a.id) ?? [],
        });
        this.items = this.items.filter((i) => i !== item);
        await cache.removeOutbox(item.client_message_id);
        this.emit({ type: "sent", item, response });
        this.emit({ type: "changed" });
      } catch (error) {
        if (isTransient(error)) return; // offline or server trouble: stop, retry later
        if (error instanceof ApiError && error.code === "run_in_progress") {
          blocked.add(item.conversation_id); // retried once that reply finishes
          continue;
        }
        item.status = "failed";
        item.error = error instanceof Error ? error.message : String(error);
        await cache.putOutbox(item);
        blocked.add(item.conversation_id);
        this.emit({ type: "changed" });
      }
    }
  }

  private emit(event: OutboxEvent) {
    for (const listener of this.listeners) listener(event);
  }
}

export const outbox = new Outbox();
