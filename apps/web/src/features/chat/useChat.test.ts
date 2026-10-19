import { describe, expect, it } from "vitest";

import type { RunEvent } from "../../lib/types";
import { applyEvent, type LiveRun } from "./useChat";

const base: LiveRun = {
  runId: "r1",
  messageId: "m1",
  text: "",
  reasoning: "",
  status: "queued",
  error: null,
  toolCalls: {},
};

const ev = (type: RunEvent["type"], data: Record<string, unknown> = {}, run_id = "r1"): RunEvent => ({
  event_id: 1,
  run_id,
  type,
  created_at: "",
  data,
});

describe("applyEvent", () => {
  it("accumulates text and reasoning deltas", () => {
    let live = applyEvent(base, ev("run.started", { message_id: "m1" }));
    live = applyEvent(live, ev("message.delta", { reasoning: "hmm " }));
    live = applyEvent(live, ev("message.delta", { text: "Hel" }));
    live = applyEvent(live, ev("message.delta", { text: "lo", reasoning: "ok" }));
    expect(live).toMatchObject({ text: "Hello", reasoning: "hmm ok", status: "running" });
  });

  it("discards partial output when the worker restarts the run", () => {
    const live = applyEvent({ ...base, text: "partial", status: "running" }, ev("run.progress", { stage: "restarted" }));
    expect(live).toMatchObject({ text: "", reasoning: "", status: "queued" });
  });

  it("records terminal states", () => {
    expect(applyEvent(base, ev("run.completed"))?.status).toBe("completed");
    expect(applyEvent(base, ev("run.cancelled"))?.status).toBe("cancelled");
    expect(applyEvent(base, ev("run.failed", { message: "boom" }))).toMatchObject({
      status: "failed",
      error: "boom",
    });
  });

  it("ignores events for other runs", () => {
    expect(applyEvent(base, ev("message.delta", { text: "x" }, "other"))).toBe(base);
  });

  it("tracks tool calls and resets the step's text once it is saved", () => {
    const call = { id: "t1", status: "pending_approval" };
    let live = applyEvent({ ...base, status: "running", text: "Let me check" },
      ev("tool_call.proposed", { tool_call: call }));
    expect(live?.text).toBe("");
    expect(live?.toolCalls.t1).toEqual(call);
    live = applyEvent(live, ev("run.progress", { stage: "awaiting_approval" }));
    expect(live?.status).toBe("awaiting_approval");
    live = applyEvent(live, ev("tool_call.completed", { tool_call: { id: "t1", status: "completed" } }));
    expect(live?.toolCalls.t1.status).toBe("completed");
    live = applyEvent(live, ev("run.progress", { stage: "resumed" }));
    expect(live?.status).toBe("running");
  });
});
