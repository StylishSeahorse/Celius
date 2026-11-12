import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { Enrollment, RemoteAgent, RemoteAgentConfig, RemoteSession } from "../../lib/types";
import {
  ADAPTER_LABEL,
  OS_LABEL,
  SESSION_LABEL,
  STATE_DOT,
  STATE_LABEL,
  ago,
  osIcon,
  percent,
  resourcesOf,
} from "./status";

const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";
const danger =
  "rounded-md border border-red-300 px-2.5 py-1 text-sm text-red-700 hover:bg-red-50 disabled:opacity-40 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/40";

function message(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

/** Remote machines: register, authorise, configure, revoke, and see what runs where. */
export function RemoteMachinesPanel({
  onClose,
  onOpenConversation,
  onChanged,
}: {
  onClose: () => void;
  onOpenConversation: (id: string) => void;
  onChanged: () => void;
}) {
  const [agents, setAgents] = useState<RemoteAgent[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.remoteAgents().then(
      (page) => {
        setAgents(page.items);
        setError(null);
      },
      (err) => setError(message(err)),
    );
  }, []);
  useEffect(() => {
    refresh();
    // Machine state is live: connections, load and sessions change all the time.
    const timer = setInterval(refresh, 5_000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (openId) setOpenId(null);
      else if (adding) setAdding(false);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, openId, adding]);

  const open = agents?.find((a) => a.id === openId) ?? null;
  const changed = () => {
    refresh();
    onChanged();
  };

  return (
    <div className="fixed inset-0 z-30 flex items-stretch justify-center bg-black/40 sm:items-center sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="remote-title"
        className="flex h-full w-full max-w-3xl flex-col bg-canvas shadow-xl sm:h-[85vh] sm:rounded-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          {(open || adding) && (
            <button
              className={button}
              onClick={() => (setOpenId(null), setAdding(false))}
              aria-label="Back to machines"
            >
              ←
            </button>
          )}
          <h2 id="remote-title" className="flex-1 font-semibold">
            {open ? open.name : adding ? "Add a machine" : "Remote machines"}
          </h2>
          {!open && !adding && (
            <button className={primary} onClick={() => setAdding(true)}>
              Add a machine
            </button>
          )}
          <button onClick={onClose} className={button} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="flex-1 overflow-y-auto p-4">
          {error && (
            <p role="alert" className="mb-3 text-sm text-red-600 dark:text-red-400">
              {error}
            </p>
          )}
          {adding ? (
            <AddMachine onDone={() => (setAdding(false), changed())} />
          ) : open ? (
            <MachineDetail
              agent={open}
              onChanged={changed}
              onRemoved={() => (setOpenId(null), changed())}
              onOpenConversation={onOpenConversation}
            />
          ) : agents === null ? (
            <p className="text-muted">Loading…</p>
          ) : agents.length === 0 ? (
            <Intro onAdd={() => setAdding(true)} />
          ) : (
            <ul className="space-y-2">
              {agents.map((agent) => (
                <li key={agent.id}>
                  <MachineRow agent={agent} onOpen={() => setOpenId(agent.id)} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function Intro({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="mx-auto max-w-md space-y-3 py-8 text-center text-sm text-muted">
      <p className="text-base text-ink">Run Celius on your own computers and servers.</p>
      <p>
        Install the Celius agent on a Windows PC or a Linux machine. It connects out to this server
        (nothing on the machine is opened to the network), and chats you start there show here like
        any other conversation, on every device.
      </p>
      <button className={primary} onClick={onAdd}>
        Add a machine
      </button>
    </div>
  );
}

function StateBadge({ state }: { state: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <span className={`h-2 w-2 rounded-full ${STATE_DOT[state] ?? "bg-muted"}`} aria-hidden="true" />
      {STATE_LABEL[state] ?? state}
    </span>
  );
}

function MachineRow({ agent, onOpen }: { agent: RemoteAgent; onOpen: () => void }) {
  const r = resourcesOf(agent.resources);
  const active = agent.active_sessions ?? [];
  return (
    <button
      onClick={onOpen}
      className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-line px-4 py-3 text-left hover:border-accent/60"
    >
      <span className="text-xl" aria-hidden="true">
        {osIcon(agent.os)}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{agent.name}</span>
        <StateBadge state={agent.state} />
      </span>
      {agent.online && r && (
        <span className="text-xs text-muted" title="CPU · memory in use">
          CPU {percent(r.cpu_percent)} · RAM {percent(r.memory?.percent)}
          {r.gpus?.length ? ` · GPU ${percent(r.gpus[0].utilization_percent)}` : ""}
        </span>
      )}
      <span className="text-xs text-muted">
        {active.length
          ? `${active.length} session${active.length > 1 ? "s" : ""}`
          : agent.online
            ? ""
            : `Last seen ${ago(agent.last_seen_at)}`}
      </span>
    </button>
  );
}

// ---------------------------------------------------------------------- adding

function AddMachine({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      setEnrollment(await api.createEnrollment(name.trim() || undefined));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  if (!enrollment) {
    return (
      <div className="max-w-lg space-y-4 text-sm">
        <p>
          You'll get a one-time pairing code to enter on the machine. After it registers, you approve
          it here before it can be used.
        </p>
        <label className="block space-y-1">
          <span className="text-muted">Name (optional)</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Windows workstation"
            maxLength={100}
            className="w-full rounded-md border border-line bg-canvas px-2 py-1.5 outline-none focus:border-accent"
          />
        </label>
        {error && <p className="text-red-600 dark:text-red-400">{error}</p>}
        <button className={primary} disabled={busy} onClick={() => void create()}>
          Create pairing code
        </button>
      </div>
    );
  }
  const expires = new Date(enrollment.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="space-y-4 text-sm">
      <p>
        Pairing code (valid until {expires}, usable once):{" "}
        <code className="rounded bg-panel px-2 py-1 font-mono text-base tracking-wider">{enrollment.code}</code>
      </p>
      <Steps title="Windows 10/11 (PowerShell)">
        <Command text={`.\\install-windows.ps1 -Server ${serverOf(enrollment.command)} -Code ${enrollment.code}`} />
        <p className="text-muted">
          From the agent package (services/remote-agent/install). It installs the agent for your user
          and starts it at sign-in.
        </p>
      </Steps>
      <Steps title="Linux (systemd)">
        <Command text={`sudo ./install-linux.sh --server ${serverOf(enrollment.command)} --code ${enrollment.code}`} />
      </Steps>
      <Steps title="Already installed">
        <Command text={enrollment.command} />
      </Steps>
      <p className="text-muted">
        Then list the folders Celius may use in the agent's <code>policy.json</code> (nothing is allowed
        until you do), and approve the machine in this list.
      </p>
      <button className={primary} onClick={onDone}>
        Done
      </button>
    </div>
  );
}

function serverOf(command: string): string {
  return command.match(/--server (\S+)/)?.[1] ?? "";
}

function Steps({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h3 className="font-medium">{title}</h3>
      {children}
    </section>
  );
}

function Command({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-start gap-2">
      <pre className="min-w-0 flex-1 overflow-x-auto rounded-md border border-line bg-panel px-3 py-2 font-mono text-xs">
        {text}
      </pre>
      <button
        className={button}
        onClick={() =>
          void navigator.clipboard?.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          })
        }
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------- one machine

function MachineDetail({
  agent,
  onChanged,
  onRemoved,
  onOpenConversation,
}: {
  agent: RemoteAgent;
  onChanged: () => void;
  onRemoved: () => void;
  onOpenConversation: (id: string) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sessions, setSessions] = useState<RemoteSession[] | null>(null);
  const [name, setName] = useState(agent.name);
  const config = agent.config as RemoteAgentConfig;
  const local = agent.local_policy as {
    roots?: { path: string; write: boolean }[];
    allow_shell?: boolean;
    allow_write?: boolean;
    adapters?: string[];
  };
  const r = resourcesOf(agent.resources);

  useEffect(() => {
    api.remoteAgentSessions(agent.id).then((page) => setSessions(page.items), () => setSessions([]));
  }, [agent.id, agent.active_sessions?.length]);

  async function act(action: () => Promise<unknown>, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  const saveConfig = (changes: Partial<RemoteAgentConfig>) =>
    act(() => api.updateRemoteAgent(agent.id, { config: { ...config, ...changes } }));

  return (
    <div className="space-y-6 text-sm">
      {error && <p className="text-red-600 dark:text-red-400">{error}</p>}
      <section className="flex flex-wrap items-center gap-3">
        <StateBadge state={agent.state} />
        <span className="text-muted">
          {osIcon(agent.os)} {OS_LABEL[agent.os ?? ""] ?? agent.os ?? "Unknown OS"}
          {agent.arch ? ` · ${agent.arch}` : ""} · agent {agent.agent_version ?? "?"}
        </span>
        <span className="text-muted">
          {agent.online ? `Connected since ${ago(agent.connected_at)}` : `Last seen ${ago(agent.last_seen_at)}`}
        </span>
      </section>

      {agent.status === "pending" && (
        <section className="space-y-2 rounded-xl border border-amber-300 bg-amber-50/60 p-4 dark:border-amber-800 dark:bg-amber-950/20">
          <p>
            <strong>{agent.hostname ?? agent.name}</strong> registered with a pairing code and is waiting for you.
            Approve it only if you just set it up.
          </p>
          <button className={primary} disabled={busy} onClick={() => void act(() => api.authorizeRemoteAgent(agent.id))}>
            Authorise this machine
          </button>
        </section>
      )}

      {agent.online && r && (
        <section aria-label="Resources" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Gauge label="CPU" value={percent(r.cpu_percent)} detail={r.cpu_count ? `${r.cpu_count} cores` : undefined} />
          <Gauge
            label="Memory"
            value={percent(r.memory?.percent)}
            detail={r.memory ? `${(r.memory.available_mb / 1024).toFixed(1)} GB free` : undefined}
          />
          <Gauge
            label="Disk"
            value={r.disks?.[0] ? `${r.disks[0].free_gb} GB` : "–"}
            detail={r.disks?.[0] ? `free of ${r.disks[0].total_gb} GB` : undefined}
          />
          <Gauge
            label="GPU"
            value={r.gpus?.length ? percent(r.gpus[0].utilization_percent) : "none"}
            detail={
              r.gpus?.[0]
                ? `${r.gpus[0].name} · ${Math.round(r.gpus[0].memory_used_mb)}/${Math.round(r.gpus[0].memory_total_mb)} MB`
                : undefined
            }
          />
        </section>
      )}

      <section className="space-y-2">
        <h3 className="font-medium">Sessions</h3>
        {sessions === null ? (
          <p className="text-muted">Loading…</p>
        ) : sessions.length === 0 ? (
          <p className="text-muted">Nothing has run here yet. Start a new chat and choose this machine.</p>
        ) : (
          <ul className="divide-y divide-line rounded-xl border border-line">
            {sessions.map((s) => (
              <li key={s.id}>
                <button
                  onClick={() => onOpenConversation(s.conversation_id)}
                  className="flex w-full flex-wrap items-center gap-x-3 px-3 py-2 text-left hover:bg-panel"
                >
                  <span className="min-w-0 flex-1 truncate">{s.conversation_title || "Untitled"}</span>
                  <span className="text-xs text-muted">{ADAPTER_LABEL[s.adapter] ?? s.adapter}</span>
                  <span className="text-xs text-muted">{SESSION_LABEL[s.status] ?? s.status}</span>
                  <span className="w-full truncate font-mono text-xs text-muted">{s.working_directory}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-2">
        <h3 className="font-medium">What this machine allows</h3>
        <p className="text-muted">
          Set on the machine itself, in the agent's policy file. Celius can only narrow it.
        </p>
        <ul className="space-y-1">
          <li>
            Folders:{" "}
            {local.roots?.length ? (
              local.roots.map((root) => (
                <code key={root.path} className="mr-2 rounded bg-panel px-1.5 py-0.5 text-xs">
                  {root.path}
                  {root.write ? "" : " (read only)"}
                </code>
              ))
            ) : (
              <span className="text-amber-700 dark:text-amber-300">none yet: sessions can't open</span>
            )}
          </li>
          <li>Commands (shell, Python, git): {local.allow_shell ? "allowed" : "not allowed"}</li>
          <li>Writing files: {local.allow_write ? "allowed" : "not allowed"}</li>
          <li>Agent types: {(local.adapters ?? ["celius"]).map((a) => ADAPTER_LABEL[a] ?? a).join(", ")}</li>
        </ul>
      </section>

      {agent.status !== "revoked" && (
        <section className="space-y-3">
          <h3 className="font-medium">Settings in Celius</h3>
          <label className="flex items-center gap-2">
            <span className="w-28 text-muted">Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              className="min-w-0 flex-1 rounded-md border border-line bg-canvas px-2 py-1 outline-none focus:border-accent"
            />
            <button
              className={button}
              disabled={busy || !name.trim() || name === agent.name}
              onClick={() => void act(() => api.updateRemoteAgent(agent.id, { name: name.trim() }))}
            >
              Rename
            </button>
          </label>
          <fieldset className="space-y-1.5">
            <legend className="mb-1 text-muted">Before changes and commands</legend>
            <label className="flex items-start gap-2">
              <input
                type="radio"
                checked={config.approval !== "profile"}
                onChange={() => void saveConfig({ approval: "always" })}
                disabled={busy}
                className="mt-1"
              />
              <span>Always ask me (recommended for a real computer)</span>
            </label>
            <label className="flex items-start gap-2">
              <input
                type="radio"
                checked={config.approval === "profile"}
                onChange={() => void saveConfig({ approval: "profile" })}
                disabled={busy}
                className="mt-1"
              />
              <span>Follow the chat's tool profile, as in the sandbox</span>
            </label>
          </fieldset>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={config.allow_shell ?? true}
              onChange={(e) => void saveConfig({ allow_shell: e.target.checked })}
              disabled={busy}
            />
            Allow commands (when the machine allows them)
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={config.allow_write ?? true}
              onChange={(e) => void saveConfig({ allow_write: e.target.checked })}
              disabled={busy}
            />
            Allow writing files
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={(config.adapters ?? []).includes("claude_code")}
              onChange={(e) =>
                void saveConfig({
                  adapters: e.target.checked ? ["celius", "claude_code"] : ["celius"],
                })
              }
              disabled={busy}
            />
            Allow Claude Code sessions
            {!agent.capabilities?.claude_code && <span className="text-muted">(not installed there)</span>}
          </label>
        </section>
      )}

      <section className="flex flex-wrap gap-2 border-t border-line pt-4">
        {agent.online && (
          <button
            className={button}
            disabled={busy}
            onClick={() => void act(() => api.disconnectRemoteAgent(agent.id))}
            title="The agent reconnects at once; running work continues"
          >
            Reconnect agent
          </button>
        )}
        {agent.status !== "revoked" && (
          <button
            className={danger}
            disabled={busy}
            onClick={() =>
              void act(
                () => api.revokeRemoteAgent(agent.id),
                `Revoke ${agent.name}? Its credential stops working at once and its sessions end. To use it again, register it again.`,
              )
            }
          >
            Revoke
          </button>
        )}
        <button
          className={danger}
          disabled={busy}
          onClick={() =>
            void act(
              async () => {
                await api.deleteRemoteAgent(agent.id);
                onRemoved();
              },
              `Remove ${agent.name} from Celius? Its conversations stay.`,
            )
          }
        >
          Remove
        </button>
      </section>
    </div>
  );
}

function Gauge({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-xl border border-line px-3 py-2">
      <div className="text-xs text-muted">{label}</div>
      <div className="text-lg font-medium">{value}</div>
      {detail && <div className="truncate text-xs text-muted" title={detail}>{detail}</div>}
    </div>
  );
}
