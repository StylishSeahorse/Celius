import { describe, expect, it, vi } from "vitest";

import { SseParser, followRun } from "./sse";
import type { RunEvent } from "./types";

const event = (id: number, type: string, data: object = {}) =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({
    event_id: id,
    run_id: "r",
    type,
    created_at: "2026-10-19T00:00:00Z",
    data,
  })}\n\n`;

function streamOf(...chunks: string[]): Response {
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { headers: { "content-type": "text/event-stream" } });
}

describe("SseParser", () => {
  it("handles events split across chunks, comments and retry lines", () => {
    const parser = new SseParser();
    const whole = "retry: 2000\n\n: keepalive\n\n" + event(1, "run.started") + event(2, "message.delta", { text: "hi" });
    const out: RunEvent[] = [];
    for (let i = 0; i < whole.length; i += 7) out.push(...parser.push(whole.slice(i, i + 7)));
    expect(out.map((e) => e.event_id)).toEqual([1, 2]);
    expect(out[1].data).toEqual({ text: "hi" });
  });

  it("accepts CRLF line endings", () => {
    const parser = new SseParser();
    expect(parser.push(event(3, "run.completed").replace(/\n/g, "\r\n"))).toHaveLength(1);
  });
});

describe("followRun", () => {
  it("resumes from the last event id after a dropped connection", async () => {
    const urls: string[] = [];
    const responses = [
      () => streamOf(event(1, "run.started"), event(2, "message.delta", { text: "Hel" })),
      () => Promise.reject(new TypeError("network down")),
      () => streamOf(event(3, "message.delta", { text: "lo" }), event(4, "run.completed")),
    ];
    const fetchImpl = vi.fn(async (url: string | URL | Request) => {
      urls.push(String(url));
      if (String(url).endsWith("/runs/r")) {
        return new Response(JSON.stringify({ status: "running" }));
      }
      return responses.shift()!();
    }) as unknown as typeof fetch;

    const seen: number[] = [];
    await followRun("r", { fetchImpl, onEvent: (e) => seen.push(e.event_id), maxBackoffMs: 1 });

    expect(seen).toEqual([1, 2, 3, 4]);
    const eventUrls = urls.filter((u) => u.includes("/events"));
    expect(eventUrls).toEqual([
      "/api/v1/runs/r/events?after=0",
      "/api/v1/runs/r/events?after=2",
      "/api/v1/runs/r/events?after=2",
    ]);
  });

  it("stops when the stream closes and the run is already over", async () => {
    const fetchImpl = vi.fn(async (url: string | URL | Request) =>
      String(url).endsWith("/runs/r")
        ? new Response(JSON.stringify({ status: "completed" }))
        : streamOf("retry: 2000\n\n"),
    ) as unknown as typeof fetch;
    await followRun("r", { fetchImpl, onEvent: () => {}, after: 9 });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("does not retry authorisation failures", async () => {
    const fetchImpl = vi.fn(async () => new Response("", { status: 401 })) as unknown as typeof fetch;
    await expect(followRun("r", { fetchImpl, onEvent: () => {} })).rejects.toThrow("401");
  });
});
