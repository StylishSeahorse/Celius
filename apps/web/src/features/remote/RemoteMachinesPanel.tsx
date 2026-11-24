import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { DeviceRequest, RemoteAgent, RemoteAgentConfig, RemoteSession } from "../../lib/types";
import {
  ORIGIN_LABEL,
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
  initialCode,
}: {
  onClose: () => void;
  onOpenConversation: (id: string) => void;
  onChanged: () => void;
  /** From a device's sign-in link (#/devices?code=…). */
  initialCode?: string | null;
}) {
  const [agents, setAgents] = useState<RemoteAgent[] | null>(null);
  const [waiting, setWaiting] = useState<DeviceRequest[]>([]);
  const [commands, setCommands] = useState<DeviceCommands>({
    login: "celius login --server …",
    linux: "curl -fsSL …/install.sh | sudo bash",
    windows: "irm …/install.ps1 | iex",
  });
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState(Boolean(initialCode));
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.remoteAgents().then(
      (page) => {
        setAgents(page.items);
        setError(null);
      },
      (err) => setError(message(err)),
    );
    api.deviceRequests().then(
      (page) => {
        setWaiting(page.items);
        setCommands({ login: page.login_command, linux: page.install_linux, windows: page.install_windows });
      },
      () => undefined,
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
            {open ? open.name : adding ? "Add a device" : "Remote machines"}
          </h2>
          {!open && !adding && (
            <button className={primary} onClick={() => setAdding(true)}>
              Add a device
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
            <AddDevice
              commands={commands}
              initialCode={initialCode ?? ""}
              onAccepted={() => (setAdding(false), changed())}
            />
          ) : open ? (
            <MachineDetail
              agent={open}
              onChanged={changed}
              onRemoved={() => (setOpenId(null), changed())}
              onOpenConversation={onOpenConversation}
            />
          ) : agents === null ? (
            <p className="text-muted">Loading…</p>
          ) : agents.length === 0 && waiting.length === 0 ? (
            <Intro onAdd={() => setAdding(true)} />
          ) : (
            <>
              {waiting.length > 0 && (
                <section className="mb-4 space-y-2" aria-label="Devices waiting for you">
                  <h3 className="text-sm font-medium">Waiting for you to accept</h3>
                  {waiting.map((request) => (
                    <WaitingDevice key={request.id} request={request} onDecided={changed} />
                  ))}
                </section>
              )}
              <ul className="space-y-2">
              {agents.map((agent) => (
                <li key={agent.id}>
                  <MachineRow agent={agent} onOpen={() => setOpenId(agent.id)} />
                </li>
              ))}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Intro({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="mx-auto max-w-md space-y-3 py-8 text-center text-sm text-muted">
      <p className="text-base text-ink">Use Celius on your own computers and servers.</p>
      <p>
        Install Celius on a Windows PC or a Linux machine and sign it in from there with{" "}
        <code>celius login</code>; you accept it here. Then chat with it from any device, or run{" "}
        <code>celius</code> in a terminal on the machine itself. It connects out to this server:
        nothing on the machine is opened to the network.
      </p>
      <button className={primary} onClick={onAdd}>
        Add a device
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

function WaitingDevice({ request, onDecided }: { request: DeviceRequest; onDecided: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function decide(accept: boolean) {
    setBusy(true);
    setError(null);
    try {
      if (accept) await api.acceptDevice(request.user_code);
      else await api.refuseDevice(request.user_code);
      onDecided();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-amber-300 bg-amber-50/60 px-4 py-3 dark:border-amber-800 dark:bg-amber-950/20">
      <span className="text-xl" aria-hidden="true">
        {osIcon(request.os)}
      </span>
      <span className="min-w-0 flex-1 text-sm">
        <span className="block font-medium">{request.name || request.hostname || "A device"}</span>
        <span className="text-xs text-muted">
          {OS_LABEL[request.os ?? ""] ?? request.os ?? ""}
          {request.ip_address ? ` · from ${request.ip_address}` : ""} · {ago(request.created_at)}
        </span>
      </span>
      <span className="text-sm">
        Code <code className="rounded bg-canvas px-1.5 py-0.5 font-mono tracking-wider">{request.user_code}</code>
      </span>
      <button className={primary} disabled={busy} onClick={() => void decide(true)}>
        Accept
      </button>
      <button className={button} disabled={busy} onClick={() => void decide(false)}>
        Refuse
      </button>
      {error && <p className="w-full text-xs text-red-600 dark:text-red-400">{error}</p>}
      <p className="w-full text-xs text-muted">Accept only if this code is the one shown on your device right now.</p>
    </div>
  );
}

type DeviceCommands = { login: string; linux: string; windows: string };

function AddDevice({
  commands,
  initialCode,
  onAccepted,
}: {
  commands: DeviceCommands;
  initialCode: string;
  onAccepted: () => void;
}) {
  const [code, setCode] = useState(initialCode);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      await api.acceptDevice(code.trim(), name.trim() || undefined);
      onAccepted();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5 text-sm">
      <Steps title="1. Install Celius on the device">
        <p className="text-muted">
          Run one command on the device. It installs Celius (and Python, if needed), then shows a code and
          waits for you here.
        </p>
        <p className="text-xs text-muted">Windows — in PowerShell, not as administrator:</p>
        <Command text={commands.windows} />
        <p className="text-xs text-muted">Linux:</p>
        <Command text={commands.linux} />
      </Steps>
      <Steps title="2. Or, if Celius is already installed there, sign it in">
        <Command text={commands.login} />
        <p className="text-muted">
          It shows a code. With <code>--user</code> the device then waits for you in this list; otherwise type its
          code below.
        </p>
      </Steps>
      <Steps title="3. Accept it here">
        <div className="flex flex-wrap items-end gap-2">
          <label className="space-y-1">
            <span className="block text-muted">Code shown on the device</span>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="BCDF-GHJK"
              maxLength={20}
              className="w-36 rounded-md border border-line bg-canvas px-2 py-1.5 font-mono tracking-wider outline-none focus:border-accent"
            />
          </label>
          <label className="min-w-0 flex-1 space-y-1">
            <span className="block text-muted">Name (optional)</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Windows workstation"
              maxLength={100}
              className="w-full rounded-md border border-line bg-canvas px-2 py-1.5 outline-none focus:border-accent"
            />
          </label>
          <button
            className={primary}
            disabled={busy || code.replace(/[^A-Za-z0-9]/g, "").length < 8}
            onClick={() => void accept()}
          >
            Accept device
          </button>
        </div>
        {error && <p className="text-red-600 dark:text-red-400">{error}</p>}
      </Steps>
      <p className="text-muted">
        Then run <code>celius</code> in a folder on the device (it asks you once before working there), or list
        folders in its <code>policy.json</code> to use it from here.
      </p>
    </div>
  );
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
                  <span className="text-xs text-muted">{ORIGIN_LABEL[s.origin] ?? s.origin}</span>
                  <span className="text-xs text-muted">{SESSION_LABEL[s.status] ?? s.status}</span>
                  <span className="w-full truncate font-mono text-xs text-muted">{s.working_directory}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <MachinePolicy agent={agent} onChanged={onChanged} />

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

type LocalPolicy = {
  roots?: { path: string; write: boolean }[];
  allow_shell?: boolean;
  allow_write?: boolean;
  allow_remote_edit?: boolean | null;
};

/** The machine's own policy file: what Celius may do there at all. Edited here, checked and
 * saved by the machine (which can lock it with allow_remote_edit: false). */
function MachinePolicy({ agent, onChanged }: { agent: RemoteAgent; onChanged: () => void }) {
  const local = agent.local_policy as LocalPolicy;
  const [roots, setRoots] = useState(local.roots ?? []);
  const [shell, setShell] = useState(Boolean(local.allow_shell));
  const [write, setWrite] = useState(Boolean(local.allow_write));
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const key = JSON.stringify([local.roots ?? [], Boolean(local.allow_shell), Boolean(local.allow_write)]);

  useEffect(() => {
    // The machine changed it (or a save came back): show what it has now.
    setRoots(local.roots ?? []);
    setShell(Boolean(local.allow_shell));
    setWrite(Boolean(local.allow_write));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const locked = local.allow_remote_edit === false;
  const outdated = local.allow_remote_edit == null;
  const editable = agent.status === "active" && !locked && !outdated;
  const dirty = JSON.stringify([roots, shell, write]) !== key;
  const windows = agent.os === "windows";
  const candidate = adding.trim();
  const absolute = windows ? /^[A-Za-z]:[\\/]|^\\\\/.test(candidate) : candidate.startsWith("/") || candidate.startsWith("~");

  function add() {
    if (!absolute || roots.some((r) => r.path === candidate)) return;
    setRoots([...roots, { path: candidate, write: true }]);
    setAdding("");
    setSaved(false);
  }

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await api.updateMachinePolicy(agent.id, { roots, allow_shell: shell, allow_write: write });
      setSaved(true);
      onChanged();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3" aria-label="What this machine allows">
      <h3 className="font-medium">What this machine allows</h3>
      <p className="text-muted">
        The machine's own policy file: Celius can't go beyond it. Changes are checked and saved by the machine
        and apply from the next command, also in chats already running there.
      </p>
      {locked && (
        <p className="text-amber-700 dark:text-amber-300">
          Locked on the machine: change these in its policy file (allow_remote_edit is false).
        </p>
      )}
      {outdated && agent.status === "active" && (
        <p className="text-amber-700 dark:text-amber-300">
          Update Celius on this machine to change these from here (run its install command again with --upgrade).
        </p>
      )}

      <div className="space-y-1.5">
        <span className="block text-muted">Folders</span>
        {roots.length === 0 && (
          <p className="text-amber-700 dark:text-amber-300">None yet: sessions can't open on this machine.</p>
        )}
        <ul className="space-y-1">
          {roots.map((root, index) => (
            <li key={root.path} className="flex flex-wrap items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded bg-panel px-1.5 py-0.5 text-xs" title={root.path}>
                {root.path}
              </code>
              {editable ? (
                <>
                  <select
                    aria-label={`Access to ${root.path}`}
                    value={root.write ? "write" : "read"}
                    onChange={(e) => {
                      setRoots(roots.map((r, i) => (i === index ? { ...r, write: e.target.value === "write" } : r)));
                      setSaved(false);
                    }}
                    className="rounded-md border border-line bg-canvas px-1.5 py-0.5 text-xs"
                  >
                    <option value="write">Read and change</option>
                    <option value="read">Read only</option>
                  </select>
                  <button
                    className={button}
                    aria-label={`Remove ${root.path}`}
                    onClick={() => (setRoots(roots.filter((_, i) => i !== index)), setSaved(false))}
                  >
                    Remove
                  </button>
                </>
              ) : (
                <span className="text-xs text-muted">{root.write ? "read and change" : "read only"}</span>
              )}
            </li>
          ))}
        </ul>
        {editable && (
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={adding}
              onChange={(e) => setAdding(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && add()}
              placeholder={windows ? "C:\\Users\\me\\code" : "/home/me/code"}
              aria-label="Folder to add"
              className="min-w-0 flex-1 rounded-md border border-line bg-canvas px-2 py-1 font-mono text-xs outline-none focus:border-accent"
            />
            <button className={button} disabled={!absolute} onClick={add}>
              Add folder
            </button>
          </div>
        )}
      </div>

      <label className="flex items-center gap-2">
        <input type="checkbox" checked={shell} disabled={!editable} onChange={(e) => (setShell(e.target.checked), setSaved(false))} />
        Commands (shell, Python, git)
      </label>
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={write} disabled={!editable} onChange={(e) => (setWrite(e.target.checked), setSaved(false))} />
        Creating and changing files
      </label>

      {editable && (
        <div className="flex flex-wrap items-center gap-2">
          <button className={primary} disabled={!dirty || busy || !agent.online} onClick={() => void save()}>
            {busy ? "Saving on the machine…" : "Save on the machine"}
          </button>
          {dirty && (
            <button
              className={button}
              disabled={busy}
              onClick={() => (setRoots(local.roots ?? []), setShell(Boolean(local.allow_shell)), setWrite(Boolean(local.allow_write)))}
            >
              Undo changes
            </button>
          )}
          {!agent.online && <span className="text-xs text-muted">The machine must be online to save.</span>}
          {saved && !dirty && <span className="text-xs text-green-700 dark:text-green-400">Saved on the machine.</span>}
        </div>
      )}
      {error && <p className="text-red-600 dark:text-red-400">{error}</p>}
    </section>
  );
}
