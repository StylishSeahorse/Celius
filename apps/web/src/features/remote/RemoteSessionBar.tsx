import { useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { RemoteSession, RemoteSessionSummary } from "../../lib/types";
import { SessionMonitor } from "./SessionMonitor";
import { ADAPTER_LABEL, OS_LABEL, SESSION_LABEL, osIcon } from "./status";

const button = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-canvas disabled:opacity-40";

/** Shown on a conversation that runs on a machine: where, its state, and its controls. */
export function RemoteSessionBar({
  summary,
  conversationId,
  online,
  onChanged,
}: {
  summary: RemoteSessionSummary;
  conversationId: string;
  online: boolean;
  onChanged: () => void;
}) {
  const [session, setSession] = useState<RemoteSession | null>(null);
  const [monitor, setMonitor] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.remoteSession(summary.id).then(setSession, () => undefined);
  }, [summary.id, summary.status]);

  const ended = summary.status === "terminated" || summary.status === "failed";
  const working = summary.status === "running" || summary.status === "awaiting_approval";
  const machineState = !summary.agent_online && !ended ? " · machine offline (work there continues; updates arrive when it's back)" : "";

  async function act(action: () => Promise<unknown>, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    }
  }

  return (
    <div
      role="status"
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-1.5 text-xs ${
        summary.status === "awaiting_approval" ? "bg-amber-50/70 dark:bg-amber-950/25" : "bg-panel/60"
      }`}
    >
      <span className="font-medium">
        {osIcon(summary.agent_os)} {summary.agent_name ?? "Removed machine"}
      </span>
      <span className="text-muted">
        {OS_LABEL[summary.agent_os ?? ""] ?? ""} · {ADAPTER_LABEL[summary.adapter] ?? summary.adapter}
      </span>
      <span className="max-w-[16rem] truncate font-mono text-muted" title={summary.working_directory}>
        {summary.working_directory}
      </span>
      <span className="flex-1">
        <span className={summary.status === "failed" ? "text-red-700 dark:text-red-300" : ""}>
          {SESSION_LABEL[summary.status] ?? summary.status}
        </span>
        {summary.status_detail && ended ? `: ${summary.status_detail}` : ""}
        <span className="text-muted">{machineState}</span>
      </span>
      {error && <span className="text-red-700 dark:text-red-300">{error}</span>}
      <button className={button} disabled={!session} onClick={() => setMonitor(true)}>
        Terminal & files
      </button>
      {working && (
        <button
          className={button}
          disabled={!online}
          onClick={() => void act(() => api.interruptRemoteSession(summary.id))}
        >
          Interrupt
        </button>
      )}
      {!ended ? (
        <button
          className={button}
          disabled={!online}
          onClick={() =>
            void act(
              () => api.terminateRemoteSession(summary.id),
              "End this session on the machine? The conversation stays; you can open a session again.",
            )
          }
        >
          End session
        </button>
      ) : (
        summary.agent_id &&
        summary.mode === "native" && (
          <button
            className={button}
            disabled={!online}
            onClick={() =>
              void act(() =>
                api.createRemoteSession({
                  conversation_id: conversationId,
                  agent_id: summary.agent_id!,
                  adapter: "celius",
                  working_directory: summary.working_directory,
                }),
              )
            }
          >
            Open again
          </button>
        )
      )}
      {monitor && session && <SessionMonitor session={{ ...session, status: summary.status }} onClose={() => setMonitor(false)} />}
    </div>
  );
}
