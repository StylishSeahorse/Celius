import { type ReactNode, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type {
  ActivityItem,
  McpServer,
  PendingApproval,
  Schedule,
  ToolInfo,
  ToolProfile,
  ToolSettings,
} from "../../lib/types";
import { ToolCallCard, toolLabel } from "./ToolCallCard";
import { loadProfiles, useToolProfiles } from "./ToolProfilePicker";

type Tab = "approvals" | "activity" | "schedules" | "settings" | "mcp";

const input = "w-full rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent";
const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

export function ToolsPanel({
  isAdmin,
  initialTab = "approvals",
  onOpenConversation,
  onClose,
  onPendingChanged,
}: {
  isAdmin: boolean;
  initialTab?: Tab;
  onOpenConversation: (id: string) => void;
  onClose: () => void;
  onPendingChanged: () => void;
}) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const tabs: [Tab, string][] = [
    ["approvals", "Approvals"],
    ["activity", "Activity"],
    ["schedules", "Scheduled"],
    ["settings", "Permissions"],
    ...(isAdmin ? ([["mcp", "MCP servers"]] as [Tab, string][]) : []),
  ];
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-30 flex items-stretch justify-center bg-black/40 sm:items-center sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="tools-title"
        className="flex h-full w-full max-w-3xl flex-col bg-canvas shadow-xl sm:h-[85vh] sm:rounded-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <h2 id="tools-title" className="flex-1 font-semibold">
            Tools
          </h2>
          <button onClick={onClose} className={button} aria-label="Close">
            ✕
          </button>
        </header>
        <nav className="flex gap-1 overflow-x-auto border-b border-line px-3 py-2" aria-label="Tools sections">
          {tabs.map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              aria-current={tab === key ? "page" : undefined}
              className={`shrink-0 rounded-md px-3 py-1 text-sm ${tab === key ? "bg-panel font-medium" : "text-muted hover:text-ink"}`}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="flex-1 overflow-y-auto p-4">
          {tab === "approvals" && (
            <Approvals onOpenConversation={onOpenConversation} onChanged={onPendingChanged} />
          )}
          {tab === "activity" && <Activity onOpenConversation={onOpenConversation} />}
          {tab === "schedules" && <Schedules onOpenConversation={onOpenConversation} />}
          {tab === "settings" && <Permissions />}
          {tab === "mcp" && isAdmin && <McpServers />}
        </div>
      </div>
    </div>
  );
}

function useLoad<T>(load: () => Promise<T>, intervalMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    load().then(setData, (err) => setError(message(err)));
  }, [load]);
  useEffect(() => {
    reload();
    if (!intervalMs) return;
    const timer = setInterval(reload, intervalMs);
    return () => clearInterval(timer);
  }, [reload, intervalMs]);
  return { data, error, reload, setError };
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-muted">{children}</p>;
}

// ---------------------------------------------------------------------- approvals

function Approvals({
  onOpenConversation,
  onChanged,
}: {
  onOpenConversation: (id: string) => void;
  onChanged: () => void;
}) {
  const { data, error, reload } = useLoad<PendingApproval[]>(api.pendingApprovals, 5000);
  if (error) return <p role="alert">{error}</p>;
  if (!data) return <p className="text-muted">Loading…</p>;
  if (!data.length) return <Empty>Nothing is waiting for your approval.</Empty>;
  return (
    <ul className="space-y-4">
      {data.map((call) => (
        <li key={call.id} className="space-y-1">
          <button className="text-sm text-muted underline" onClick={() => onOpenConversation(call.conversation_id)}>
            {call.conversation_title || "Untitled chat"}
          </button>
          <ToolCallCard
            call={call}
            onDecided={() => {
              reload();
              onChanged();
            }}
          />
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------- activity

const STATUS_TONE: Record<string, string> = {
  running: "text-accent",
  queued: "text-muted",
  awaiting_approval: "text-amber-700 dark:text-amber-400",
  failed: "text-red-600 dark:text-red-400",
};

function Activity({ onOpenConversation }: { onOpenConversation: (id: string) => void }) {
  const load = useCallback(() => api.activity().then((r) => r.items), []);
  const { data, error, reload, setError } = useLoad<ActivityItem[]>(load, 4000);
  if (error) return <p role="alert">{error}</p>;
  if (!data) return <p className="text-muted">Loading…</p>;
  if (!data.length) return <Empty>No activity yet.</Empty>;
  return (
    <ul className="divide-y divide-line">
      {data.map((item) => (
        <li key={`${item.kind}-${item.id}`} className="flex items-center gap-3 py-2 text-sm" data-testid="activity-item">
          <div className="min-w-0 flex-1">
            {item.conversation_id ? (
              <button className="truncate text-left hover:underline" onClick={() => onOpenConversation(item.conversation_id!)}>
                {item.title}
              </button>
            ) : (
              <span className="truncate">{item.title}</span>
            )}
            <p className="text-xs text-muted">
              {item.kind === "run" ? (item.source === "schedule" ? "Scheduled reply" : "Reply") : "Background job"}
              {item.tool_calls ? ` · ${item.tool_calls} tool call${item.tool_calls > 1 ? "s" : ""}` : ""}
              {" · "}
              {new Date(item.created_at).toLocaleString()}
              {item.error ? ` · ${item.error}` : ""}
            </p>
          </div>
          <span className={`text-xs ${STATUS_TONE[item.status] ?? "text-muted"}`}>
            {item.status.replace(/_/g, " ")}
            {item.pending_approvals ? ` (${item.pending_approvals})` : ""}
          </span>
          {item.cancellable && (
            <button
              className={button}
              onClick={() =>
                void api.cancelActivity(item.kind, item.id).then(reload, (err) => setError(message(err)))
              }
            >
              Cancel
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------- schedules

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function describe(s: Schedule): string {
  if (s.kind === "interval") return `Every ${s.interval_minutes} min`;
  const days = s.kind === "weekly" ? (s.weekdays ?? []).map((d) => WEEKDAYS[d]).join(", ") : "Daily";
  return `${days} at ${s.time_of_day} (${s.timezone})`;
}

function Schedules({ onOpenConversation }: { onOpenConversation: (id: string) => void }) {
  const { data, error, reload, setError } = useLoad<Schedule[]>(api.schedules);
  const [creating, setCreating] = useState(false);
  const act = (p: Promise<unknown>) => void p.then(reload, (err) => setError(message(err)));
  return (
    <div className="space-y-4">
      {error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <p className="text-sm text-muted">
        A scheduled task sends its prompt to its own chat on a schedule. Tool calls that need
        approval wait for you, like in any chat.
      </p>
      {data?.map((s) => (
        <div key={s.id} className="space-y-1 rounded-lg border border-line p-3 text-sm" data-testid="schedule">
          <div className="flex items-center gap-2">
            <span className="flex-1 font-medium">{s.name}</span>
            <label className="flex items-center gap-1 text-xs">
              <input
                type="checkbox"
                checked={s.enabled}
                onChange={(e) => act(api.updateSchedule(s.id, { enabled: e.target.checked }))}
              />
              On
            </label>
            <button className={button} onClick={() => act(api.runSchedule(s.id))}>
              Run now
            </button>
            <button className={button} onClick={() => confirm(`Delete "${s.name}"?`) && act(api.deleteSchedule(s.id))}>
              Delete
            </button>
          </div>
          <p className="text-xs text-muted">
            {describe(s)}
            {s.next_run_at && s.enabled ? ` · next ${new Date(s.next_run_at).toLocaleString()}` : ""}
            {s.last_status ? ` · last: ${s.last_status}` : ""}
            {s.last_error ? ` (${s.last_error})` : ""}
          </p>
          <p className="line-clamp-2 text-xs whitespace-pre-wrap">{s.prompt}</p>
          {s.conversation_id && (
            <button className="text-xs underline" onClick={() => onOpenConversation(s.conversation_id!)}>
              Open its chat
            </button>
          )}
        </div>
      ))}
      {data && !data.length && !creating && <Empty>No scheduled tasks.</Empty>}
      {creating ? (
        <ScheduleForm
          onDone={() => {
            setCreating(false);
            reload();
          }}
          onCancel={() => setCreating(false)}
        />
      ) : (
        <button className={primary} onClick={() => setCreating(true)}>
          New scheduled task
        </button>
      )}
    </div>
  );
}

function ScheduleForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const profiles = useToolProfiles();
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [kind, setKind] = useState<"interval" | "daily" | "weekly">("daily");
  const [interval, setIntervalMinutes] = useState(60);
  const [time, setTime] = useState("08:00");
  const [weekdays, setWeekdays] = useState<number[]>([0]);
  const [profile, setProfile] = useState<string>("read_only");
  const [error, setError] = useState<string | null>(null);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

  async function submit() {
    setError(null);
    try {
      await api.createSchedule({
        name,
        prompt,
        kind,
        interval_minutes: kind === "interval" ? interval : null,
        time_of_day: kind === "interval" ? null : time,
        weekdays: kind === "weekly" ? weekdays : [],
        timezone,
        tool_profile: profile || null,
        enabled: true,
      });
      onDone();
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <div className="space-y-2 rounded-lg border border-line p-3 text-sm">
      <input className={input} placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} aria-label="Task name" />
      <textarea
        className={input}
        rows={3}
        placeholder="Prompt, e.g. Summarise the news on example.com"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        aria-label="Prompt"
      />
      <div className="flex flex-wrap items-center gap-2">
        <select className="rounded-md border border-line bg-canvas px-2 py-1" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} aria-label="Repeat">
          <option value="interval">Every N minutes</option>
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
        </select>
        {kind === "interval" ? (
          <input type="number" min={15} className="w-24 rounded-md border border-line bg-canvas px-2 py-1" value={interval}
            onChange={(e) => setIntervalMinutes(Number(e.target.value))} aria-label="Minutes" />
        ) : (
          <input type="time" className="rounded-md border border-line bg-canvas px-2 py-1" value={time} onChange={(e) => setTime(e.target.value)} aria-label="Time" />
        )}
        <span className="text-xs text-muted">{timezone}</span>
      </div>
      {kind === "weekly" && (
        <div className="flex flex-wrap gap-1">
          {WEEKDAYS.map((label, day) => (
            <label key={label} className="flex items-center gap-1 text-xs">
              <input
                type="checkbox"
                checked={weekdays.includes(day)}
                onChange={(e) => setWeekdays((d) => (e.target.checked ? [...d, day] : d.filter((x) => x !== day)))}
              />
              {label}
            </label>
          ))}
        </div>
      )}
      <label className="flex items-center gap-2 text-xs">
        Tool permissions
        <select className="rounded-md border border-line bg-canvas px-2 py-1 text-sm" value={profile} onChange={(e) => setProfile(e.target.value)}>
          {profiles.map((p) => (
            <option key={p.key} value={p.key}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      <div className="flex justify-end gap-2">
        <button className={button} onClick={onCancel}>
          Cancel
        </button>
        <button className={primary} disabled={!name.trim() || !prompt.trim()} onClick={() => void submit()}>
          Create
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------- permissions

const DECISIONS = ["allow", "ask", "deny"] as const;

function Permissions() {
  const load = useCallback(
    () => Promise.all([api.toolSettings(), api.tools(), loadProfiles(true)]),
    [],
  );
  const { data, error, reload, setError } = useLoad<[ToolSettings, ToolInfo[], ToolProfile[]]>(load);
  const [editing, setEditing] = useState<ToolProfile | "new" | null>(null);
  if (error) return <p role="alert">{error}</p>;
  if (!data) return <p className="text-muted">Loading…</p>;
  const [settings, tools, profiles] = data;
  const changed = () => {
    window.dispatchEvent(new Event("celius:profiles-changed"));
    reload();
  };
  const save = (body: { default_profile?: string; disabled_tools?: string[] }) =>
    void api.updateToolSettings(body).then(reload, (err) => setError(message(err)));

  return (
    <div className="space-y-6 text-sm">
      <section className="space-y-2">
        <h3 className="font-medium">Sandbox</h3>
        <p className="text-muted">
          {settings.sandbox
            ? settings.sandbox.status === "ok"
              ? `Running (${settings.sandbox.mode === "bwrap" ? "with per-run namespaces" : "container isolation"}). Code has no network access.`
              : "Configured but not reachable. Code tools fail until it is back."
            : "Not configured: Python, shell, Git and document tools are off. See the deployment guide."}
        </p>
        <p className="text-xs text-muted">
          Up to {settings.limits.max_iterations} tool steps and {Math.round(settings.limits.max_run_seconds / 60)} min of
          tool time per reply; approvals expire after {settings.limits.approval_ttl_minutes} min.
          {settings.network_enabled
            ? settings.web_allowlist.length
              ? ` Web: only ${settings.web_allowlist.join(", ")}.`
              : " Web: public sites only (never your local network)."
            : " Web access is off."}
        </p>
      </section>

      <section className="space-y-2">
        <h3 className="font-medium">Default for new chats</h3>
        <select
          className="rounded-md border border-line bg-canvas px-2 py-1"
          value={settings.default_profile}
          onChange={(e) => save({ default_profile: e.target.value })}
          aria-label="Default tool profile"
        >
          {profiles.map((p) => (
            <option key={p.key} value={p.key}>
              {p.label}
            </option>
          ))}
        </select>
        <p className="text-xs text-muted">{profiles.find((p) => p.key === settings.default_profile)?.description}</p>
      </section>

      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <h3 className="flex-1 font-medium">Profiles</h3>
          <button className={button} onClick={() => setEditing("new")}>
            New profile
          </button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted">
                <th className="py-1 pr-2 font-normal">Tool</th>
                {profiles.map((p) => (
                  <th key={p.key} className="px-1 py-1 font-normal">
                    {p.builtin ? (
                      p.label
                    ) : (
                      <button className="underline" onClick={() => setEditing(p)}>
                        {p.label}
                      </button>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tools.map((t) => (
                <tr key={t.name} className="border-t border-line/60">
                  <td className="py-1 pr-2 capitalize">{toolLabel(t.name)}</td>
                  {profiles.map((p) => (
                    <td key={p.key} className={`px-1 ${p.decisions[t.name] === "deny" ? "text-muted" : ""}`}>
                      {p.decisions[t.name]}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {editing && (
          <ProfileEditor
            profile={editing === "new" ? null : editing}
            tools={tools}
            onDone={() => {
              setEditing(null);
              changed();
            }}
            onCancel={() => setEditing(null)}
          />
        )}
      </section>

      <section className="space-y-2">
        <h3 className="font-medium">Tools</h3>
        <ul className="space-y-1">
          {tools.map((t) => {
            const off = settings.disabled_tools.includes(t.name);
            return (
              <li key={t.name} className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={!off}
                  aria-label={`Enable ${t.label}`}
                  onChange={() =>
                    save({
                      disabled_tools: off
                        ? settings.disabled_tools.filter((n) => n !== t.name)
                        : [...settings.disabled_tools, t.name],
                    })
                  }
                />
                <div>
                  <p>
                    <span className="font-medium">{t.label}</span>{" "}
                    <span className="text-xs text-muted">({t.risk})</span>
                    {!t.available && t.unavailable_reason && !off && (
                      <span className="text-xs text-muted"> · {t.unavailable_reason}</span>
                    )}
                  </p>
                  <p className="text-xs text-muted">{t.description}</p>
                </div>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}

function ProfileEditor({
  profile,
  tools,
  onDone,
  onCancel,
}: {
  profile: ToolProfile | null;
  tools: ToolInfo[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(profile?.label ?? "");
  const [base, setBase] = useState(profile?.base ?? "standard");
  const [rules, setRules] = useState<Record<string, string>>(profile?.rules ?? {});
  const [error, setError] = useState<string | null>(null);
  const id = profile?.key.replace("custom:", "");

  async function submit() {
    try {
      if (id) await api.updateToolProfile(id, { name, base, rules });
      else await api.createToolProfile({ name, base, rules });
      onDone();
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <div className="space-y-2 rounded-lg border border-line p-3">
      <div className="flex flex-wrap gap-2">
        <input className={`${input} max-w-60`} placeholder="Profile name" value={name} onChange={(e) => setName(e.target.value)} aria-label="Profile name" />
        <label className="flex items-center gap-1 text-xs">
          Based on
          <select className="rounded-md border border-line bg-canvas px-2 py-1 text-sm" value={base} onChange={(e) => setBase(e.target.value)}>
            {["read_only", "standard", "developer", "administrator"].map((b) => (
              <option key={b} value={b}>
                {b.replace("_", " ")}
              </option>
            ))}
          </select>
        </label>
      </div>
      <ul className="grid gap-1 sm:grid-cols-2">
        {tools.map((t) => (
          <li key={t.name} className="flex items-center gap-2 text-xs">
            <span className="flex-1 capitalize">{toolLabel(t.name)}</span>
            <select
              value={rules[t.name] ?? ""}
              onChange={(e) =>
                setRules(({ [t.name]: _old, ...rest }) => (e.target.value ? { ...rest, [t.name]: e.target.value } : rest))
              }
              className="rounded border border-line bg-canvas px-1 py-0.5"
              aria-label={`${t.label} rule`}
            >
              <option value="">(from base)</option>
              {DECISIONS.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </li>
        ))}
      </ul>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      <div className="flex justify-end gap-2">
        {id && (
          <button
            className={button}
            onClick={() => void api.deleteToolProfile(id).then(onDone, (err) => setError(message(err)))}
          >
            Delete
          </button>
        )}
        <button className={button} onClick={onCancel}>
          Cancel
        </button>
        <button className={primary} disabled={!name.trim()} onClick={() => void submit()}>
          Save
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------- MCP

function McpServers() {
  const { data, error, reload, setError } = useLoad<McpServer[]>(api.mcpServers);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [lan, setLan] = useState(false);
  const act = (p: Promise<unknown>) => void p.then(reload, (err) => setError(message(err)));

  return (
    <div className="space-y-4 text-sm">
      <p className="text-muted">
        MCP servers add tools from other services (streamable HTTP). Only you can add them; the
        model never can. Their tools follow your permission profiles (default: ask first).
      </p>
      {error && <p role="alert" className="text-red-600 dark:text-red-400">{error}</p>}
      {data?.map((s) => (
        <div key={s.id} className="space-y-1 rounded-lg border border-line p-3">
          <div className="flex items-center gap-2">
            <span className="flex-1 font-medium">{s.name}</span>
            <label className="flex items-center gap-1 text-xs">
              <input type="checkbox" checked={s.enabled} onChange={(e) => act(api.updateMcpServer(s.id, { enabled: e.target.checked }))} />
              On
            </label>
            <button className={button} onClick={() => act(api.refreshMcpServer(s.id))}>
              Refresh
            </button>
            <button className={button} onClick={() => confirm(`Remove ${s.name}?`) && act(api.deleteMcpServer(s.id))}>
              Remove
            </button>
          </div>
          <p className="truncate text-xs text-muted">
            {s.url}
            {s.has_credentials ? " · with credentials" : ""}
            {s.allow_private_network ? " · local network allowed" : ""}
          </p>
          {s.last_error ? (
            <p className="text-xs text-red-600 dark:text-red-400">{s.last_error}</p>
          ) : (
            <p className="text-xs">{s.tools.map((t) => String(t.name)).join(", ") || "No tools"}</p>
          )}
        </div>
      ))}
      <div className="space-y-2 rounded-lg border border-line p-3">
        <div className="grid gap-2 sm:grid-cols-2">
          <input className={input} placeholder="name (a-z, 0-9, -)" value={name} onChange={(e) => setName(e.target.value)} aria-label="Server name" />
          <input className={input} placeholder="https://…/mcp" value={url} onChange={(e) => setUrl(e.target.value)} aria-label="Server URL" />
        </div>
        <input
          className={input}
          type="password"
          placeholder="Authorization header value (optional), e.g. Bearer …"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-label="Authorization"
        />
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={lan} onChange={(e) => setLan(e.target.checked)} />
          The server is on my local network (allows a private address)
        </label>
        <div className="flex justify-end">
          <button
            className={primary}
            disabled={!name || !url}
            onClick={() =>
              act(
                api
                  .createMcpServer({
                    name,
                    url,
                    allow_private_network: lan,
                    headers: token ? { Authorization: token } : null,
                  })
                  .then(() => {
                    setName("");
                    setUrl("");
                    setToken("");
                  }),
              )
            }
          >
            Add server
          </button>
        </div>
      </div>
    </div>
  );
}
