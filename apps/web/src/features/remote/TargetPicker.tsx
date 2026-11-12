import { useEffect, useState } from "react";

import { api, type RemoteTarget } from "../../lib/api";
import type { RemoteAgent } from "../../lib/types";
import { ADAPTER_LABEL, STATE_DOT, STATE_LABEL, osIcon } from "./status";

const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";

/** Where a new chat runs: Celius's sandbox (default) or one of the user's machines. */
export function TargetPicker({
  value,
  onChange,
  disabled,
}: {
  value: RemoteTarget | null;
  onChange: (target: RemoteTarget | null) => void;
  disabled?: boolean;
}) {
  const [agents, setAgents] = useState<RemoteAgent[]>([]);
  const [choosing, setChoosing] = useState<RemoteAgent | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.remoteAgents().then(
        (page) => alive && setAgents(page.items.filter((a) => a.status === "active")),
        () => undefined,
      );
    void load();
    const timer = setInterval(load, 15_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  if (agents.length === 0 && !value) return null; // no machines: nothing to choose
  const selected = value ? agents.find((a) => a.id === value.agent_id) : null;

  return (
    <>
      <select
        aria-label="Where this chat runs"
        title={value ? `${value.working_directory} (${ADAPTER_LABEL[value.adapter]})` : "Celius sandbox"}
        disabled={disabled}
        value={value?.agent_id ?? ""}
        onChange={(e) => {
          const agent = agents.find((a) => a.id === e.target.value);
          if (!agent) onChange(null);
          else setChoosing(agent);
        }}
        className="max-w-[12rem] min-w-0 truncate rounded-md border border-line bg-canvas px-2 py-1 text-sm"
      >
        <option value="">Celius sandbox</option>
        {agents.map((a) => (
          <option key={a.id} value={a.id}>
            {osIcon(a.os)} {a.name}
            {a.online ? "" : " (offline)"}
          </option>
        ))}
      </select>
      {selected && value && (
        <span className="hidden max-w-[10rem] truncate font-mono text-xs text-muted lg:inline" title={value.working_directory}>
          {value.working_directory}
        </span>
      )}
      {choosing && (
        <ChooseFolder
          agent={choosing}
          initial={value?.agent_id === choosing.id ? value : null}
          onCancel={() => setChoosing(null)}
          onChoose={(target) => {
            onChange(target);
            setChoosing(null);
          }}
        />
      )}
    </>
  );
}

function ChooseFolder({
  agent,
  initial,
  onChoose,
  onCancel,
}: {
  agent: RemoteAgent;
  initial: RemoteTarget | null;
  onChoose: (target: RemoteTarget) => void;
  onCancel: () => void;
}) {
  const roots = ((agent.local_policy as { roots?: { path: string; write: boolean }[] }).roots ?? []).map(
    (r) => r.path,
  );
  const [directory, setDirectory] = useState(initial?.working_directory ?? roots[0] ?? "");
  const adapters = (agent.permissions as { adapters?: string[] }).adapters ?? ["celius"];
  const claudeAvailable = adapters.includes("claude_code") && Boolean(agent.capabilities?.claude_code);
  const [adapter, setAdapter] = useState<RemoteTarget["adapter"]>(initial?.adapter ?? "celius");
  const windows = agent.os === "windows";
  const absolute = windows ? /^[A-Za-z]:[\\/]|^\\\\/.test(directory) : directory.startsWith("/");

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4" onClick={onCancel}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="target-title"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md space-y-4 rounded-2xl bg-canvas p-5 text-sm shadow-xl"
      >
        <h2 id="target-title" className="font-semibold">
          Run on {agent.name}
        </h2>
        <p className="flex items-center gap-1.5 text-xs text-muted">
          <span className={`h-2 w-2 rounded-full ${STATE_DOT[agent.state] ?? "bg-muted"}`} aria-hidden="true" />
          {STATE_LABEL[agent.state] ?? agent.state}
          {!agent.online && " · work starts when it's back"}
        </p>
        {roots.length === 0 ? (
          <p className="text-amber-700 dark:text-amber-300">
            This machine allows no folders yet. Add one to the agent's policy file on the machine (roots),
            then restart the agent.
          </p>
        ) : (
          <label className="block space-y-1">
            <span className="text-muted">Folder</span>
            <input
              list={`roots-${agent.id}`}
              value={directory}
              onChange={(e) => setDirectory(e.target.value)}
              placeholder={windows ? "C:\\Users\\me\\code\\project" : "/home/me/code/project"}
              className="w-full rounded-md border border-line bg-canvas px-2 py-1.5 font-mono text-xs outline-none focus:border-accent"
            />
            <datalist id={`roots-${agent.id}`}>
              {roots.map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
            <span className="block text-xs text-muted">Inside: {roots.join(", ")}</span>
          </label>
        )}
        <fieldset className="space-y-1.5">
          <legend className="mb-1 text-muted">Agent</legend>
          <label className="flex items-start gap-2">
            <input type="radio" checked={adapter === "celius"} onChange={() => setAdapter("celius")} className="mt-1" />
            <span>
              Celius <span className="text-muted">— this chat's model, memory and tools, acting on the machine</span>
            </span>
          </label>
          <label className={`flex items-start gap-2 ${claudeAvailable ? "" : "opacity-50"}`}>
            <input
              type="radio"
              checked={adapter === "claude_code"}
              onChange={() => setAdapter("claude_code")}
              disabled={!claudeAvailable}
              className="mt-1"
            />
            <span>
              Claude Code{" "}
              <span className="text-muted">
                {claudeAvailable
                  ? "— runs there; its permission requests come to you here"
                  : "— not installed or not allowed on this machine"}
              </span>
            </span>
          </label>
        </fieldset>
        <div className="flex justify-end gap-2">
          <button className={button} onClick={onCancel}>
            Cancel
          </button>
          <button
            className={primary}
            disabled={!absolute || roots.length === 0}
            onClick={() => onChoose({ agent_id: agent.id, adapter, working_directory: directory.trim() })}
          >
            Use this machine
          </button>
        </div>
      </div>
    </div>
  );
}
