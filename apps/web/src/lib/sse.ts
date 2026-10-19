import { TERMINAL_EVENTS, type RunEvent } from "./types";

/** Incremental parser for a text/event-stream body. Feed it chunks; it returns whole events. */
export class SseParser {
  private buffer = "";

  push(chunk: string): RunEvent[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const events: RunEvent[] = [];
    let boundary: number;
    while ((boundary = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const data = block
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).replace(/^ /, ""))
        .join("\n");
      if (data) events.push(JSON.parse(data) as RunEvent);
    }
    return events;
  }
}

export interface StreamOptions {
  after?: number;
  signal?: AbortSignal;
  onEvent: (event: RunEvent) => void;
  /** Called while reconnecting after a dropped connection. */
  onReconnecting?: (attempt: number) => void;
  fetchImpl?: typeof fetch;
  maxBackoffMs?: number;
}

/**
 * Follow a run's events until a terminal event arrives, reconnecting with the last
 * event id after network failures (e.g. a phone switching from Wi-Fi to mobile data).
 */
export async function followRun(runId: string, options: StreamOptions): Promise<void> {
  const fetchImpl = options.fetchImpl ?? fetch;
  let cursor = options.after ?? 0;
  let attempt = 0;
  while (!options.signal?.aborted) {
    let finished = false;
    try {
      const response = await fetchImpl(`/api/v1/runs/${runId}/events?after=${cursor}`, {
        credentials: "same-origin",
        headers: { Accept: "text/event-stream" },
        signal: options.signal,
      });
      if (response.status === 401 || response.status === 404) {
        throw new Error(`Run stream unavailable (HTTP ${response.status})`);
      }
      if (!response.ok || !response.body) throw new RetryableError(`HTTP ${response.status}`);
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      const parser = new SseParser();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const event of parser.push(value)) {
          cursor = event.event_id;
          attempt = 0;
          options.onEvent(event);
          if (TERMINAL_EVENTS.has(event.type)) finished = true;
        }
      }
      // The server closes the stream only after the terminal event has been sent, so a
      // clean close without one means the run is already over (or the proxy cut us off).
      if (finished || (await runIsOver(fetchImpl, runId, options.signal))) return;
    } catch (error) {
      if (options.signal?.aborted) return;
      if (!(error instanceof RetryableError) && !(error instanceof TypeError)) throw error;
    }
    attempt += 1;
    options.onReconnecting?.(attempt);
    const delay = Math.min(500 * 2 ** (attempt - 1), options.maxBackoffMs ?? 10_000);
    await sleep(delay, options.signal);
  }
}

class RetryableError extends Error {}

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

async function runIsOver(fetchImpl: typeof fetch, runId: string, signal?: AbortSignal) {
  const response = await fetchImpl(`/api/v1/runs/${runId}`, {
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) return false;
  const run = (await response.json()) as { status: string };
  return TERMINAL_STATUSES.has(run.status);
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}
