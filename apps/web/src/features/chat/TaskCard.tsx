import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { AgentTask } from "../../lib/types";

const button = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-panel disabled:opacity-40";

const STEP_MARK: Record<string, string> = {
  pending: "○",
  in_progress: "◐",
  done: "●",
  failed: "✕",
  skipped: "–",
};

const STATUS_LABEL: Record<string, string> = {
  active: "In progress",
  paused: "Paused",
  blocked: "Blocked",
};

/** The open task of this conversation: plan, progress, blockers, checks and files. The
 * state is kept by Celius across replies (ADR C-08); it refreshes while a reply runs and
 * when another device changes it. */
export function TaskCard({
  conversationId,
  running,
  revision,
  online,
  onResumed,
}: {
  conversationId: string | null;
  running: boolean;
  revision: number;
  online: boolean;
  onResumed: () => void;
}) {
  const [task, setTask] = useState<AgentTask | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!conversationId) return setTask(null);
    api.agentTasks({ conversationId }).then(
      (tasks) => setTask(tasks[0] ?? null),
      () => undefined, // offline or gone: keep what is shown
    );
  }, [conversationId]);

  useEffect(load, [load, revision]);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [running, load]);
  useEffect(() => {
    const onChange = () => load();
    window.addEventListener("celius:agent-task-changed", onChange);
    return () => window.removeEventListener("celius:agent-task-changed", onChange);
  }, [load]);

  if (!task) return null;
  const done = task.plan.filter((s) => s.status === "done" || s.status === "skipped").length;
  const current = task.plan.find((s) => s.status === "in_progress") ?? task.plan.find((s) => s.status === "pending");
  const act = (p: Promise<unknown>, after?: () => void) =>
    void p.then(
      () => {
        setError(null);
        load();
        after?.();
      },
      (err) => setError(err instanceof ApiError ? err.message : "Something went wrong"),
    );

  return (
    <section
      aria-label="Current task"
      data-testid="task-card"
      className="mb-2 rounded-xl border border-line bg-panel/60 text-sm"
    >
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="text-xs font-medium text-muted">{STATUS_LABEL[task.status] ?? task.status}</span>
        <span className="min-w-0 flex-1 truncate">{task.objective}</span>
        {task.plan.length > 0 && (
          <span className="shrink-0 text-xs text-muted" aria-label={`${done} of ${task.plan.length} steps done`}>
            {done}/{task.plan.length}
          </span>
        )}
        <span aria-hidden="true" className="text-xs text-muted">
          {open ? "▴" : "▾"}
        </span>
      </button>
      {!open && current && task.status === "active" && (
        <p className="truncate px-3 pb-2 text-xs text-muted">Now: {current.title}</p>
      )}
      {task.status_reason && task.status !== "active" && (
        <p className="px-3 pb-2 text-xs text-amber-700 dark:text-amber-300">{task.status_reason}</p>
      )}
      {open && (
        <div className="space-y-2 border-t border-line px-3 py-2">
          {task.plan.length > 0 && (
            <ol className="space-y-0.5">
              {task.plan.map((step) => (
                <li key={step.n} className="flex gap-2" data-testid="task-step" data-status={step.status}>
                  <span aria-label={step.status.replace("_", " ")} className="w-3 shrink-0 text-muted">
                    {STEP_MARK[step.status] ?? "○"}
                  </span>
                  <span className={step.status === "done" || step.status === "skipped" ? "text-muted" : ""}>
                    {step.title}
                    {step.note && <span className="block text-xs text-muted">{step.note}</span>}
                  </span>
                </li>
              ))}
            </ol>
          )}
          {task.blockers.length > 0 && (
            <p className="text-xs">
              <span className="font-medium">Blocked by: </span>
              {task.blockers.join("; ")}
            </p>
          )}
          {task.verifications.length > 0 && (
            <ul className="text-xs">
              {task.verifications.slice(-5).map((v, i) => (
                <li key={i}>
                  {v.passed ? "✓" : "✕"} {String(v.check)}
                  {v.detail ? <span className="text-muted"> · {String(v.detail)}</span> : null}
                </li>
              ))}
            </ul>
          )}
          {task.files.length > 0 && (
            <p className="text-xs text-muted">
              Files: {task.files.slice(-10).map((f) => String(f.path)).join(", ")}
              {task.files.length > 10 ? ` and ${task.files.length - 10} more` : ""}
            </p>
          )}
          <p className="text-xs text-muted">
            {task.runs} {task.runs === 1 ? "reply" : "replies"} · {task.tool_calls} tool calls
            {task.failed_tool_calls ? ` (${task.failed_tool_calls} failed)` : ""}
            {task.verified === true ? " · verified" : task.verified === false ? " · a check failed" : ""}
          </p>
          <div className="flex flex-wrap gap-2">
            {(task.status === "paused" || task.status === "blocked") && (
              <button
                className={button}
                disabled={!online || running}
                onClick={() => act(api.resumeAgentTask(task.id), onResumed)}
              >
                Resume
              </button>
            )}
            <button
              className={button}
              disabled={!online}
              onClick={() => act(api.updateAgentTask(task.id, { status: "completed" }))}
            >
              Mark done
            </button>
            <button
              className={button}
              disabled={!online}
              onClick={() =>
                confirm("Stop this task? Celius won't continue it.") &&
                act(api.updateAgentTask(task.id, { status: "abandoned" }))
              }
            >
              Stop task
            </button>
          </div>
          {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        </div>
      )}
    </section>
  );
}
