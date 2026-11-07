import { useEffect, useState } from "react";

import { ApiError, type Autonomy, type Insights as InsightsData, api } from "../../lib/api";

const small = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-panel disabled:opacity-40";

function message(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

function pct(value: unknown) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "–";
}

function num(value: unknown) {
  return typeof value === "number" ? value.toLocaleString() : "–";
}

/** How Celius has been doing, from what it records anyway. */
export function Insights({ isAdmin }: { isAdmin: boolean }) {
  const [days, setDays] = useState(30);
  const [scope, setScope] = useState<"me" | "everyone">("me");
  const [data, setData] = useState<InsightsData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.insights(days, scope).then(setData, (err) => setError(message(err)));
  }, [days, scope]);

  const s = (section: string) => (data?.[section] ?? {}) as Record<string, unknown>;
  const runs = s("runs");
  const tools = s("tools");
  const context = s("context");
  const agent = s("agent");
  const skills = s("skills");
  const memory = s("memory");
  const assistant = s("assistant");
  const rows: [string, string][] = [
    ["Replies", `${num(runs.total)} · ${pct(runs.completion_rate)} completed · ${pct(runs.failure_rate)} failed`],
    ["Reply time", `${num(runs.duration_seconds_p50)} s typical, ${num(runs.duration_seconds_p90)} s slow (90%)`],
    ["Tool calls", `${num(tools.calls)} · ${pct(tools.success_rate)} succeeded`],
    ["Prompt size", `${num(context.prompt_tokens_p50)} tokens typical · history trimmed in ${pct(context.dropped_history_rate)}`],
    ["Multi-step tasks", `${num(agent.tasks)} · ${pct(agent.completion_rate)} completed · ${pct(agent.verified_completion_rate)} verified · ${pct(agent.recovery_rate)} recovered after an interruption`],
    ["Skills", `${num(skills.skills)} in use · reused in ${pct(skills.reuse_rate)} of tool replies · ${pct(skills.use_success_rate)} of uses worked`],
    ["Memory", `given in ${pct(memory.replies_given_memory_rate)} of replies · ${pct(memory.acceptance_rate)} of suggestions kept · ${num(memory.open_conflicts)} conflicts · ${num(memory.never_used)} never used`],
    ["Your tasks", `${num(assistant.tasks_added)} added · ${num(assistant.tasks_completed)} done · ${num(assistant.suggestions_accepted)} of ${num(assistant.suggestions)} suggestions accepted`],
    ["Automations", `${num(assistant.automation_runs)} runs · ${pct(assistant.automation_success_rate)} succeeded`],
  ];
  const byTool = (tools.by_tool as { tool: string; calls: number; success_rate: number | null }[] | undefined) ?? [];
  const reasons = Object.entries((runs.failure_reasons as Record<string, number> | undefined) ?? {});

  return (
    <section className="space-y-2" aria-label="Insights">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="flex-1 font-medium">Insights</h3>
        <select className={small} aria-label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>7 days</option>
          <option value={30}>30 days</option>
          <option value={90}>90 days</option>
        </select>
        {isAdmin && (
          <select className={small} aria-label="Whose" value={scope} onChange={(e) => setScope(e.target.value as "me" | "everyone")}>
            <option value="me">Mine</option>
            <option value="everyone">Everyone</option>
          </select>
        )}
      </div>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {data && (
        <dl className="space-y-1 text-xs" data-testid="insights">
          {rows.map(([label, value]) => (
            <div key={label} className="flex gap-2">
              <dt className="w-28 shrink-0 text-muted">{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {byTool.length > 0 && (
        <details className="text-xs">
          <summary>By tool</summary>
          <ul className="mt-1 space-y-0.5">
            {byTool.slice(0, 15).map((t) => (
              <li key={t.tool}>
                {t.tool}: {t.calls} calls, {pct(t.success_rate)} succeeded
              </li>
            ))}
          </ul>
        </details>
      )}
      {reasons.length > 0 && (
        <p className="text-xs text-muted">Failures: {reasons.map(([code, n]) => `${code} ×${n}`).join(", ")}</p>
      )}
    </section>
  );
}

const PRESETS: [Autonomy["preset"], string, string][] = [
  ["careful", "Careful", "Nothing is learned or suggested on its own; tools only read."],
  ["balanced", "Balanced", "Suggests memories, tasks and skills; you approve them. Tools ask before changing things."],
  ["hands_off", "Hands-off", "Keeps what it learns without asking; publishes tested skills; runs code in the sandbox freely."],
];

/** How much Celius does on its own, in one place. */
export function AutonomySettings() {
  const [value, setValue] = useState<Autonomy | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.autonomy().then(setValue, (err) => setError(message(err)));
  }, []);
  const save = (body: Parameters<typeof api.updateAutonomy>[0]) =>
    void api.updateAutonomy(body).then(setValue, (err) => setError(message(err)));
  if (!value) return error ? <p role="alert">{error}</p> : null;
  return (
    <section className="space-y-2" aria-label="How much Celius does on its own">
      <h3 className="font-medium">How much Celius does on its own</h3>
      <div className="flex flex-wrap gap-2">
        {PRESETS.map(([key, label, help]) => (
          <button
            key={key}
            title={help}
            aria-pressed={value.preset === key}
            className={`rounded-md border px-2.5 py-1 text-sm ${value.preset === key ? "border-accent bg-panel font-medium" : "border-line"}`}
            onClick={() => save({ preset: key as "careful" | "balanced" | "hands_off" })}
          >
            {label}
          </button>
        ))}
        {value.preset === "custom" && <span className="self-center text-xs text-muted">Custom</span>}
      </div>
      <p className="text-xs text-muted">{PRESETS.find(([k]) => k === value.preset)?.[2] ?? "Your own mix of the settings below."}</p>
      <ul className="space-y-0.5 text-xs" data-testid="autonomy">
        <li>Memory suggestions: {value.memory_auto_extract ? (value.memory_require_review ? "on, you review them" : "on, kept without asking") : "off"}</li>
        <li>Tasks from chats: {value.suggest_tasks ? "suggested" : "off"}</li>
        <li>Skills: {value.skill_trust}{value.skill_auto_generate ? ", learned from finished tasks" : ""}</li>
        <li>Tool permissions for new chats: {value.tool_profile.replace("_", " ")}</li>
        <li>Always asks first: sending email</li>
      </ul>
      {value.notes?.map((n) => (
        <p key={n} className="text-xs text-amber-700 dark:text-amber-300">
          {n}
        </p>
      ))}
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </section>
  );
}
