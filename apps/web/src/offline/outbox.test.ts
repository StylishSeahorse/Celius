import "fake-indexeddb/auto";

import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/api";
import { Outbox } from "./outbox";

type Sent = { conversation: string; client_message_id: string; content: string };

function fakeClient(behaviour: (sent: Sent, attempt: number) => "ok" | Error) {
  const delivered: Sent[] = [];
  const created: string[] = [];
  const attempts = new Map<string, number>();
  const client = {
    createConversation: vi.fn(async (body: { id?: string }) => {
      created.push(body.id!);
      return {} as never;
    }),
    sendMessage: vi.fn(async (conversation: string, body: { client_message_id: string; content: string }) => {
      const sent = { conversation, ...body };
      const n = (attempts.get(body.client_message_id) ?? 0) + 1;
      attempts.set(body.client_message_id, n);
      const outcome = behaviour(sent, n);
      if (outcome instanceof Error) throw outcome;
      delivered.push(sent);
      return { run: { id: `run-${body.client_message_id}` } } as never;
    }),
  };
  return { client, delivered, created, attempts };
}

const item = (id: string, conversation = "c1", extra = {}) => ({
  client_message_id: id,
  conversation_id: conversation,
  content: `message ${id}`,
  ...extra,
});

describe("Outbox", () => {
  it("delivers in order and removes delivered items", async () => {
    const { client, delivered } = fakeClient(() => "ok");
    const box = new Outbox(client as never);
    const sent: string[] = [];
    box.subscribe((e) => e.type === "sent" && sent.push(e.item.client_message_id));
    await box.enqueue(item("a"));
    await box.enqueue(item("b"));
    await box.flush();
    expect(delivered.map((d) => d.client_message_id)).toEqual(["a", "b"]);
    expect(sent).toEqual(["a", "b"]);
    expect(box.size).toBe(0);
  });

  it("keeps items while offline and retries with the same id", async () => {
    let offline = true;
    const { client, delivered, attempts } = fakeClient(() => (offline ? new TypeError("Failed to fetch") : "ok"));
    const box = new Outbox(client as never);
    await box.enqueue(item("q1"));
    await box.flush();
    expect(box.list()).toHaveLength(1);
    expect(box.list()[0].status).toBe("queued");

    offline = false;
    await box.flush();
    expect(delivered.map((d) => d.client_message_id)).toEqual(["q1"]);
    expect(attempts.get("q1")).toBe(3); // enqueue's flush, our offline flush, the successful one
  });

  it("survives a reload: queued items are persisted", async () => {
    const offlineClient = fakeClient(() => new TypeError("offline")).client;
    const first = new Outbox(offlineClient as never);
    await first.enqueue(item("persisted", "c-persist"));

    const { client, delivered } = fakeClient(() => "ok");
    const second = new Outbox(client as never); // a new page load
    await second.flush();
    expect(delivered.map((d) => d.client_message_id)).toContain("persisted");
  });

  it("creates an offline-started conversation before its first message", async () => {
    const { client, created, delivered } = fakeClient(() => "ok");
    const box = new Outbox(client as never);
    await box.enqueue(item("first", "new-conv", { create_conversation: { model_override: null, project_id: null } }));
    await box.flush();
    expect(created).toEqual(["new-conv"]);
    expect(delivered[0].conversation).toBe("new-conv");
  });

  it("waits for a running reply without blocking other conversations", async () => {
    let busy = true;
    const { client, delivered } = fakeClient((sent) =>
      busy && sent.conversation === "c1" ? new ApiError(409, "run_in_progress", "busy") : "ok",
    );
    const box = new Outbox(client as never);
    await box.enqueue(item("c1-a", "c1"));
    await box.enqueue(item("c1-b", "c1"));
    await box.enqueue(item("c2-a", "c2"));
    await box.flush();
    expect(delivered.map((d) => d.client_message_id)).toEqual(["c2-a"]);

    busy = false;
    await box.flush();
    expect(delivered.map((d) => d.client_message_id)).toEqual(["c2-a", "c1-a", "c1-b"]);
  });

  it("marks permanent failures and holds later messages of that conversation", async () => {
    const { client, delivered } = fakeClient((sent) =>
      sent.client_message_id === "bad" ? new ApiError(404, "not_found", "Conversation not found") : "ok",
    );
    const box = new Outbox(client as never);
    await box.enqueue(item("bad", "gone"));
    await box.enqueue(item("after-bad", "gone"));
    await box.enqueue(item("elsewhere", "c3"));
    await box.flush();
    expect(delivered.map((d) => d.client_message_id)).toEqual(["elsewhere"]);
    const failed = box.list("gone");
    expect(failed.map((i) => [i.client_message_id, i.status])).toEqual([
      ["bad", "failed"],
      ["after-bad", "queued"],
    ]);

    await box.discard("bad");
    await box.flush();
    expect(delivered.map((d) => d.client_message_id)).toEqual(["elsewhere", "after-bad"]);
  });
});
