import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../lib/api";
import type { RemoteAgent, RemoteEvent, RemoteSession } from "../../lib/types";
import { ADAPTER_LABEL, SESSION_LABEL, ago, percent, resourcesOf } from "./status";

const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const MAX_TERMINAL_CHARS = 300_000;

type Tab = "activity" | "terminal" | "files" | "machine";

/**
 * All of a session's events, then the live stream (SSE, resumed from the last sequence
 * number after any network drop). The server keeps every event, so a phone that was
 * closed during the work sees the full history when it opens this.
 */
export function useSessionEvents(sessionId: string | null) {
  const [events, setEvents] = useState<RemoteEvent[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  useEffect(() => {
    if (!sessionId) return;
    const abort = new AbortController();
    let cursor = 0;
    setEvents([]);
    const add = (items: RemoteEvent[]) => {
      const fresh = items.filter((e) => e.seq > cursor);
      if (!fresh.length) return;
      cursor = fresh[fresh.length - 1].seq;
      setEvents((current) => [...current, ...fresh]);
    };
    void (async () => {
      for (;;) {
        // Catch up (also after reconnecting), then follow.
        try {
          for (;;) {
            const page = await api.remoteSessionEvents(sessionId, cursor);
            add(page.items);
            if (!page.more) break;
          }
          const response = await fetch(`/api/v1/remote/sessions/${sessionId}/stream?after=${cursor}`, {
            credentials: "same-origin",
            headers: { Accept: "text/event-stream" },
            signal: abort.signal,
          });
          if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
          const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
          let buffer = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += value.replace(/\r\n/g, "\n");
            let boundary: number;
            while ((boundary = buffer.indexOf("\n\n")) !== -1) {
              const block = buffer.slice(0, boundary);
              buffer = buffer.slice(boundary + 2);
              const data = block
                .split("\n")
                .filter((l) => l.startsWith("data:"))
                .map((l) => l.slice(5).trimStart())
                .join("\n");
              if (!data) continue;
              const parsed = JSON.parse(data) as RemoteEvent & { status?: string };
              if (typeof parsed.seq === "number") add([parsed]);
              else if (parsed.status) setStatus(parsed.status);
            }
          }
        } catch {
          if (abort.signal.aborted) return;
        }
        if (abort.signal.aborted) return;
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    })();
    return () => abort.abort();
  }, [sessionId]);
  return { events, status };
}

export function SessionMonitor({ session, onClose }: { session: RemoteSession; onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("terminal");
  const { events } = useSessionEvents(session.id);
  const [agent, setAgent] = useState<RemoteAgent | null>(null);

  useEffect(() => {
    if (!session.agent_id) return;
    const load = () => api.remoteAgent(session.agent_id!).then(setAgent, () => undefined);
    void load();
    const timer = setInterval(load, 5_000);
    return () => clearInterval(timer);
  }, [session.agent_id]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const files = useMemo(() => latestFiles(events), [events]);
  const tabs: [Tab, string][] = [
    ["terminal", "Terminal"],
    ["activity", "Activity"],
    ["files", `Files${files.length ? ` (${files.length})` : ""}`],
    ["machine", "Machine"],
  ];

  return (
    <div className="fixed inset-0 z-30 flex items-stretch justify-center bg-black/40 sm:items-center sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="monitor-title"
        className="flex h-full w-full max-w-4xl flex-col bg-canvas shadow-xl sm:h-[88vh] sm:rounded-2xl"
      >
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="monitor-title" className="truncate font-semibold">
              {session.agent_name ?? "Machine"} · {ADAPTER_LABEL[session.adapter] ?? session.adapter}
            </h2>
            <p className="truncate font-mono text-xs text-muted" title={session.working_directory}>
              {session.working_directory}
            </p>
          </div>
          <span className="text-xs text-muted">{SESSION_LABEL[session.status] ?? session.status}</span>
          <button onClick={onClose} className={button} aria-label="Close">
            ✕
          </button>
        </header>
        <nav className="flex gap-1 overflow-x-auto border-b border-line px-3 py-2" aria-label="Session views">
          {tabs.map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              aria-current={tab === key ? "page" : undefined}
              className={`shrink-0 rounded-md px-3 py-1 text-sm ${tab === key ? "bg-panel font-medium" : "text-muted hover:bg-panel/60"}`}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="min-h-0 flex-1 overflow-hidden">
          {tab === "terminal" && <Terminal events={events} />}
          {tab === "activity" && <Activity events={events} />}
          {tab === "files" && <Files sessionId={session.id} files={files} ended={session.ended_at !== null} />}
          {tab === "machine" && <Machine agent={agent} session={session} />}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------- terminal

function Terminal({ events }: { events: RemoteEvent[] }) {
  const ref = useRef<HTMLPreElement>(null);
  const stick = useRef(true);
  const text = useMemo(() => {
    let out = "";
    let last: unknown = null;
    for (const e of events) {
      if (e.type === "command.started") {
        const tool = String(e.data.tool ?? "command");
        out += `\n\u001b ${tool}\n`;
        last = e.data.command_id;
      } else if (e.type === "terminal.output") {
        if (e.data.command_id !== last) last = e.data.command_id;
        out += String(e.data.text ?? "");
      } else if (e.type === "tool.started") {
        const input = e.data.input as Record<string, unknown> | undefined;
        const command = input && typeof input.command === "string" ? input.command : null;
        out += `\n\u001b ${String(e.data.name)}${command ? `: ${command}` : ""}\n`;
      } else if (e.type === "tool.completed" && typeof e.data.content === "string") {
        out += `${e.data.content}\n`;
      }
    }
    return out.length > MAX_TERMINAL_CHARS ? `[…earlier output not shown]\n${out.slice(-MAX_TERMINAL_CHARS)}` : out;
  }, [events]);
  useEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [text]);
  return (
    <pre
      ref={ref}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      }}
      aria-label="Terminal output"
      aria-live="off"
      className="h-full overflow-auto bg-[#151515] p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap text-[#e6e6e6]"
    >
      {text
        ? text.split("\u001b").map((chunk, i) =>
            i === 0 ? (
              chunk
            ) : (
              <span key={i}>
                <span className="text-[#d97757]">$</span>
                {chunk}
              </span>
            ),
          )
        : "No output yet."}
    </pre>
  );
}

// ---------------------------------------------------------------------- activity

const ACTIVITY: Record<string, (d: Record<string, unknown>) => string> = {
  "session.opened": () => "Session opened on the machine",
  "session.failed": (d) => `Couldn't open: ${String(d.reason ?? "")}`,
  "session.closed": () => "Session ended",
  "session.started": (d) => `Claude Code session ${String(d.external_session_id ?? "")}`,
  "command.started": (d) => `Running ${String(d.tool ?? "a command")}`,
  "command.completed": (d) => {
    const result = d.result as { is_error?: boolean } | undefined;
    if (d.status !== "completed") return `Failed: ${String(d.error ?? d.status)}`;
    return result?.is_error ? "Finished with an error" : "Finished";
  },
  "turn.started": () => "Turn started",
  "turn.completed": (d) => (d.is_error ? `Turn failed: ${String(d.result ?? "")}` : "Turn finished"),
  "tool.started": (d) => `${String(d.name)} started`,
  "tool.completed": (d) => `${d.is_error ? "Failed" : "Done"}`,
  "permission.request": (d) => `Asked to use ${String(d.tool)} — waiting for you`,
  "file.changed": (d) => `${String(d.action)} ${String(d.path)}`,
  warning: (d) => `Warning: ${String(d.message ?? "")}`,
  error: (d) => `Error: ${String(d.message ?? "")}`,
};

function Activity({ events }: { events: RemoteEvent[] }) {
  const items = events.filter((e) => e.type in ACTIVITY).slice(-500).reverse();
  if (!items.length) return <p className="p-4 text-sm text-muted">Nothing has happened yet.</p>;
  return (
    <ol className="h-full divide-y divide-line overflow-y-auto text-sm">
      {items.map((e) => (
        <li key={e.seq} className="flex gap-3 px-4 py-2">
          <time className="w-20 shrink-0 text-xs text-muted" dateTime={e.occurred_at ?? e.created_at}>
            {new Date(e.occurred_at ?? e.created_at).toLocaleTimeString()}
          </time>
          <span
            className={`min-w-0 flex-1 break-words ${
              e.type === "error" || e.type === "session.failed" ? "text-red-700 dark:text-red-300" : ""
            } ${e.type === "permission.request" ? "text-amber-700 dark:text-amber-300" : ""}`}
          >
            {ACTIVITY[e.type](e.data)}
          </span>
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------- files

interface FileChange {
  path: string;
  action: string;
  size?: number;
  diff?: string | null;
  at: string;
  count: number;
}

function latestFiles(events: RemoteEvent[]): FileChange[] {
  const byPath = new Map<string, FileChange>();
  for (const e of events) {
    if (e.type !== "file.changed") continue;
    const path = String(e.data.path ?? "");
    const previous = byPath.get(path);
    byPath.set(path, {
      path,
      action: String(e.data.action ?? "modified"),
      size: typeof e.data.size === "number" ? e.data.size : undefined,
      diff: typeof e.data.diff === "string" ? e.data.diff : (previous?.diff ?? null),
      at: e.occurred_at ?? e.created_at,
      count: (previous?.count ?? 0) + 1,
    });
  }
  return [...byPath.values()].sort((a, b) => b.at.localeCompare(a.at));
}

function Files({ sessionId, files, ended }: { sessionId: string; files: FileChange[]; ended: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!files.length) return <p className="p-4 text-sm text-muted">No files have changed yet.</p>;
  return (
    <ul className="h-full divide-y divide-line overflow-y-auto text-sm">
      {files.map((f) => (
        <li key={f.path} className="px-4 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <button
              className="min-w-0 flex-1 truncate text-left font-mono text-xs hover:underline"
              onClick={() => setOpen(open === f.path ? null : f.path)}
              aria-expanded={open === f.path}
              title={f.path}
            >
              {f.path}
            </button>
            <span className="text-xs text-muted">
              {f.action}
              {f.count > 1 ? ` ×${f.count}` : ""} · {ago(f.at)}
            </span>
            {f.action !== "deleted" && !ended && (
              <a className={button} href={api.remoteFileUrl(sessionId, f.path)} download>
                Download
              </a>
            )}
          </div>
          {open === f.path && (
            <Diff text={f.diff ?? null} />
          )}
        </li>
      ))}
    </ul>
  );
}

function Diff({ text }: { text: string | null }) {
  if (!text) return <p className="mt-2 text-xs text-muted">No diff recorded for this change (e.g. made by a command).</p>;
  return (
    <pre className="mt-2 max-h-96 overflow-auto rounded-md border border-line bg-panel p-2 font-mono text-xs">
      {text.split("\n").map((line, i) => (
        <div
          key={i}
          className={
            line.startsWith("+") && !line.startsWith("+++")
              ? "text-emerald-700 dark:text-emerald-300"
              : line.startsWith("-") && !line.startsWith("---")
                ? "text-red-700 dark:text-red-300"
                : line.startsWith("@@")
                  ? "text-sky-700 dark:text-sky-300"
                  : ""
          }
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

// ---------------------------------------------------------------------- machine

function Machine({ agent, session }: { agent: RemoteAgent | null; session: RemoteSession }) {
  const r = resourcesOf(agent?.resources);
  const permissions = session.effective_permissions as Record<string, unknown>;
  return (
    <div className="h-full space-y-4 overflow-y-auto p-4 text-sm">
      {!agent ? (
        <p className="text-muted">Loading…</p>
      ) : (
        <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1">
          <dt className="text-muted">Connection</dt>
          <dd>{agent.online ? `online since ${ago(agent.connected_at)}` : `offline · last seen ${ago(agent.last_seen_at)}`}</dd>
          <dt className="text-muted">CPU</dt>
          <dd>{percent(r?.cpu_percent)}{r?.load_average ? ` · load ${r.load_average.join(" ")}` : ""}</dd>
          <dt className="text-muted">Memory</dt>
          <dd>{r?.memory ? `${percent(r.memory.percent)} used · ${(r.memory.available_mb / 1024).toFixed(1)} GB free` : "–"}</dd>
          <dt className="text-muted">Disk</dt>
          <dd>{r?.disks?.map((d) => `${d.path}: ${d.free_gb} of ${d.total_gb} GB free`).join("; ") || "–"}</dd>
          <dt className="text-muted">GPU</dt>
          <dd>
            {r?.gpus?.length
              ? r.gpus.map((g) => `${g.name} ${percent(g.utilization_percent)} · ${Math.round(g.memory_used_mb)}/${Math.round(g.memory_total_mb)} MB`).join("; ")
              : "none reported"}
          </dd>
          <dt className="text-muted">Running</dt>
          <dd>{r?.running_commands ?? 0} command(s) · {r?.open_sessions ?? 0} open session(s)</dd>
        </dl>
      )}
      <section>
        <h3 className="mb-1 font-medium">In this session</h3>
        <ul className="text-muted">
          <li>Commands: {permissions.allow_shell ? "allowed" : "not allowed"}</li>
          <li>Writing files: {permissions.allow_write ? "allowed" : "not allowed"}</li>
          <li>
            Approvals: {permissions.approval === "profile" ? "as the chat's tool profile decides" : "asked for every change and command"}
          </li>
        </ul>
      </section>
    </div>
  );
}
