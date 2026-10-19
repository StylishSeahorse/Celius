import { useState } from "react";

import { ApiError, api, workspaceUrl } from "../../lib/api";
import type { ToolCall } from "../../lib/types";
import { formatBytes } from "../../lib/uploads";

const STATUS_LABEL: Record<string, string> = {
  pending_approval: "Needs your approval",
  approved: "Approved",
  running: "Running…",
  completed: "Done",
  failed: "Failed",
  denied: "Denied",
  expired: "Expired",
  refused: "Not allowed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

const RISK_LABEL: Record<string, string> = {
  read: "reads files",
  write: "changes files",
  execute: "runs code",
  network: "uses the web",
  external: "external service",
};

export function toolLabel(name: string): string {
  if (name.startsWith("mcp__")) {
    const [, server, tool] = name.split("__");
    return `${server}: ${tool}`;
  }
  return name.replace(/_/g, " ");
}

/** The argument that matters most, shown as code (the snippet or command to run). */
function mainCode(call: ToolCall): { label: string; code: string } | null {
  const args = call.arguments as Record<string, unknown>;
  if (typeof args.code === "string") return { label: "Python", code: args.code };
  if (typeof args.command === "string") return { label: "Shell", code: args.command };
  if (Array.isArray(args.args)) return { label: "git", code: `git ${(args.args as string[]).join(" ")}` };
  return null;
}

interface ChangedFile {
  path: string;
  size: number;
}

export function ToolCallCard({
  call,
  onDecided,
}: {
  call: ToolCall;
  onDecided?: () => void;
}) {
  const pending = call.status === "pending_approval";
  const [open, setOpen] = useState(pending);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const code = mainCode(call);
  const otherArgs = Object.fromEntries(
    Object.entries(call.arguments).filter(([k]) => !code || !["code", "command", "args"].includes(k)),
  );
  const result = (call.result ?? {}) as Record<string, unknown>;
  const changed = (result.changed_files as ChangedFile[] | undefined) ?? [];

  async function decide(kind: "once" | "tool" | "all" | "deny") {
    setBusy(true);
    setError(null);
    try {
      if (kind === "deny") await api.denyToolCall(call.id);
      // The hash proves the approval is for exactly the arguments shown here.
      else await api.approveToolCall(call.id, call.args_hash, kind);
      onDecided?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send the decision");
    } finally {
      setBusy(false);
    }
  }

  const tone = pending
    ? "border-amber-400/70 bg-amber-50/60 dark:bg-amber-950/20"
    : call.is_error
      ? "border-red-300/60 dark:border-red-900/60"
      : "border-line";

  return (
    <div data-testid="tool-call" data-status={call.status} className={`rounded-lg border text-sm ${tone}`}>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <span aria-hidden="true">{call.status === "running" ? "⏳" : "🛠"}</span>
        <span className="font-medium capitalize">{toolLabel(call.tool_name)}</span>
        <span className="text-xs text-muted">{RISK_LABEL[call.risk] ?? call.risk}</span>
        <span className="flex-1" />
        <span className={`text-xs ${pending ? "font-medium text-amber-700 dark:text-amber-400" : "text-muted"}`}>
          {STATUS_LABEL[call.status] ?? call.status}
          {call.duration_ms != null && call.status === "completed" ? ` · ${(call.duration_ms / 1000).toFixed(1)}s` : ""}
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-line/70 px-3 py-2">
          {code && (
            <div>
              <p className="mb-1 text-xs text-muted">{code.label}</p>
              <pre className="max-h-72 overflow-auto rounded bg-panel p-2 text-xs">{code.code}</pre>
            </div>
          )}
          {Object.keys(otherArgs).length > 0 && (
            <pre className="max-h-60 overflow-auto rounded bg-panel p-2 text-xs" aria-label="Arguments">
              {JSON.stringify(otherArgs, null, 2)}
            </pre>
          )}
          {pending && (
            <div className="space-y-1">
              <p className="text-xs text-muted">
                {call.decision_reason}. It will run exactly as shown
                {call.expires_at ? `; the request expires at ${new Date(call.expires_at).toLocaleTimeString()}` : ""}.
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  disabled={busy}
                  onClick={() => void decide("once")}
                  className="rounded-md bg-accent px-3 py-1 text-white disabled:opacity-40"
                >
                  Allow once
                </button>
                <button
                  disabled={busy}
                  onClick={() => void decide("tool")}
                  className="rounded-md border border-line px-3 py-1 hover:bg-panel disabled:opacity-40"
                  title="Don't ask again for this tool in this chat"
                >
                  Always allow {toolLabel(call.tool_name)}
                </button>
                <button
                  disabled={busy}
                  onClick={() => void decide("all")}
                  className="rounded-md border border-line px-3 py-1 hover:bg-panel disabled:opacity-40"
                  title="Run every tool in this chat without asking (what the permission profile forbids stays forbidden)"
                >
                  Allow all for this chat
                </button>
                <button
                  disabled={busy}
                  onClick={() => void decide("deny")}
                  className="rounded-md border border-line px-3 py-1 hover:bg-panel disabled:opacity-40"
                >
                  Deny
                </button>
              </div>
              {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
            </div>
          )}
          {!pending && call.decision_reason && ["refused", "denied", "expired"].includes(call.status) && (
            <p className="text-xs text-muted">{call.decision_reason}</p>
          )}
          <ResultView call={call} result={result} />
          {changed.length > 0 && (
            <ul className="space-y-0.5 text-xs" aria-label="Changed files">
              {changed.map((f) => (
                <li key={f.path}>
                  <a className="underline" href={workspaceUrl(call.conversation_id, f.path)} download>
                    {f.path}
                  </a>{" "}
                  <span className="text-muted">{formatBytes(f.size)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function ResultView({ call, result }: { call: ToolCall; result: Record<string, unknown> }) {
  if (!call.result) return null;
  const blocks: [string, string][] = [];
  if (typeof result.stdout === "string" && result.stdout) blocks.push(["Output", result.stdout]);
  if (typeof result.stderr === "string" && result.stderr) blocks.push(["Errors", result.stderr]);
  if (typeof result.error === "string") blocks.push(["Error", result.error]);
  return (
    <div className="space-y-1">
      {typeof result.url === "string" && (
        <p className="truncate text-xs text-muted">
          {String(result.status ?? "")} {result.url as string}
          {typeof result.title === "string" && result.title ? ` — ${result.title}` : ""}
        </p>
      )}
      {blocks.map(([label, text]) => (
        <div key={label}>
          <p className="mb-1 text-xs text-muted">{label}</p>
          <pre className="max-h-72 overflow-auto rounded bg-panel p-2 text-xs whitespace-pre-wrap">{text}</pre>
        </div>
      ))}
      {typeof result.exit_code === "number" && (
        <p className="text-xs text-muted">
          Exit code {result.exit_code as number}
          {result.killed_reason ? ` · stopped: ${String(result.killed_reason).replace(/_/g, " ")}` : ""}
        </p>
      )}
    </div>
  );
}
