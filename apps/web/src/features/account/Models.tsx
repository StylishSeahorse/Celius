import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { ModelRegistryEntry } from "../../lib/types";

const button = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-panel disabled:opacity-40";
const select = "rounded-md border border-line bg-canvas px-1.5 py-0.5 text-xs";

const PROFILES: [string, string][] = [
  ["", "Automatic (from context size)"],
  ["lite", "Lite: small models"],
  ["standard", "Standard"],
  ["advanced", "Advanced: large models"],
];
const CAPABILITIES: [string, string][] = [
  ["tool_calling", "Tool calling"],
  ["parallel_tools", "Several tool calls per turn"],
  ["structured_output", "JSON output"],
  ["vision", "Images"],
  ["reasoning_toggle", "Thinking on/off"],
];

function message(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

/** The model capability registry (administrators): what was detected, what you set, and
 * the profile each model runs with. */
export function Models() {
  const [entries, setEntries] = useState<ModelRegistryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api.modelRegistry().then(setEntries, (err) => setError(message(err)));
  }, []);
  useEffect(load, [load]);

  return (
    <section className="space-y-2" aria-label="Models">
      <h3 className="font-medium">Models</h3>
      <p className="text-xs text-muted">
        How much each model is asked to do. Capabilities come from the model server and checks; set one here only
        when you know better. Nothing is guessed from a model's name.
      </p>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {entries && !entries.length && <p className="text-xs text-muted">No models seen yet.</p>}
      <ul className="space-y-2">
        {entries?.map((entry) => (
          <ModelEntry key={`${entry.provider}/${entry.model_id}`} entry={entry} onSaved={load} />
        ))}
      </ul>
    </section>
  );
}

function ModelEntry({ entry, onSaved }: { entry: ModelRegistryEntry; onSaved: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const effective = entry.effective;
  // Local copies, so quick successive changes each build on the last one (not on the
  // entry as it was before the previous save's reload).
  const [profile, setProfile] = useState<string>(entry.profile_setting ?? "");
  const [overrides, setOverrides] = useState<Record<string, boolean | number>>(
    (entry.overrides ?? {}) as Record<string, boolean | number>,
  );
  const [limits, setLimits] = useState<Record<string, number>>((entry.limits ?? {}) as Record<string, number>);

  async function save(change: { profile?: string; overrides?: Record<string, never>; limits?: Record<string, never> }) {
    const next = {
      profile: change.profile ?? profile,
      overrides: (change.overrides ?? overrides) as Record<string, boolean | number>,
      limits: (change.limits ?? limits) as Record<string, number>,
    };
    setProfile(next.profile);
    setOverrides(next.overrides);
    setLimits(next.limits);
    setBusy(true);
    setError(null);
    try {
      await api.updateModelRegistry({
        model_id: entry.model_id,
        profile: (next.profile || null) as "lite" | "standard" | "advanced" | null,
        overrides: next.overrides as never,
        limits: next.limits as never,
        notes: entry.notes,
      });
      onSaved();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="space-y-2 rounded-lg border border-line p-3" data-testid="model-entry">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1 truncate font-mono text-xs">{entry.model_id}</span>
        <span className="text-xs text-muted">
          {effective.profile} ({effective.profile_source}) ·{" "}
          {effective.context_tokens ? `${effective.context_tokens.toLocaleString()} tokens` : "context unknown"}
        </span>
      </div>
      <label className="flex items-center gap-2 text-xs">
        Profile
        <select
          className={select}
          value={profile}
          disabled={busy}
          onChange={(e) => void save({ profile: e.target.value })}
          aria-label={`Profile for ${entry.model_id}`}
        >
          {PROFILES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
        {CAPABILITIES.map(([key, label]) => {
          const set = typeof overrides[key] === "boolean";
          const value = (effective as unknown as Record<string, boolean>)[key];
          return (
            <label key={key} className="flex items-center gap-1 text-xs">
              <select
                className={select}
                value={set ? String(overrides[key]) : ""}
                disabled={busy}
                aria-label={`${label} for ${entry.model_id}`}
                onChange={(e) => {
                  const next: Record<string, never> = { ...overrides } as Record<string, never>;
                  if (e.target.value === "") delete next[key];
                  else next[key] = (e.target.value === "true") as never;
                  void save({ overrides: next });
                }}
              >
                <option value="">auto ({value ? "yes" : "no"})</option>
                <option value="true">yes</option>
                <option value="false">no</option>
              </select>
              {label}
              <span className="text-muted">· {effective.sources?.[key] ?? "–"}</span>
            </label>
          );
        })}
      </div>
      <label className="flex items-center gap-2 text-xs">
        Tool steps per reply
        <input
          type="number"
          min={1}
          max={100}
          className={`${select} w-16`}
          defaultValue={limits.max_tool_iterations ?? ""}
          placeholder={String(effective.max_tool_iterations)}
          disabled={busy}
          aria-label={`Tool steps per reply for ${entry.model_id}`}
          onBlur={(e) => {
            const next: Record<string, never> = { ...limits } as Record<string, never>;
            if (e.target.value) next.max_tool_iterations = Number(e.target.value) as never;
            else delete next.max_tool_iterations;
            if (JSON.stringify(next) !== JSON.stringify(limits)) void save({ limits: next });
          }}
        />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <button
          className={button}
          disabled={busy}
          onClick={() => {
            setBusy(true);
            api
              .verifyModelTools(entry.model_id)
              .then(onSaved, (err) => setError(message(err)))
              .finally(() => setBusy(false));
          }}
        >
          Check tool calling
        </button>
        <span className="text-xs text-muted">
          Sends one short test prompt; the result is used by every reply.
        </span>
      </div>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </li>
  );
}
