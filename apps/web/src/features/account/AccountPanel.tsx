import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { disablePush, enablePush, type PushState, pushState } from "../../lib/notifications";

const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const input = "w-full rounded-md border border-line bg-canvas px-2 py-1.5 text-sm outline-none focus:border-accent";

interface Check {
  name: string;
  level: "ok" | "warning" | "critical";
  summary: string;
}

const LEVEL_STYLE: Record<Check["level"], string> = {
  ok: "bg-emerald-500",
  warning: "bg-amber-500",
  critical: "bg-red-500",
};

const NAMES: Record<string, string> = {
  database: "Database",
  model_server: "Model server",
  sandbox: "Sandbox",
  worker: "Worker",
  run_queue: "Replies waiting",
  job_queue: "Background jobs",
  approvals: "Approvals",
  backup: "Backups",
  disk_storage: "Disk (files)",
  disk_workspaces: "Disk (workspaces)",
  disk_backups: "Disk (backups)",
};

export function AccountPanel({ isAdmin, onClose }: { isAdmin: boolean; onClose: () => void }) {
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
        aria-labelledby="account-title"
        className="flex h-full w-full max-w-xl flex-col bg-canvas shadow-xl sm:h-auto sm:max-h-[85vh] sm:rounded-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <h2 id="account-title" className="flex-1 font-semibold">
            Account
          </h2>
          <button onClick={onClose} className={button} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="flex-1 space-y-6 overflow-y-auto p-4 text-sm">
          {isAdmin && <SystemStatus />}
          <Notifications />
          <PasswordForm />
          <Sessions />
        </div>
      </div>
    </div>
  );
}

function SystemStatus() {
  const [checks, setChecks] = useState<Check[] | null>(null);
  const [alerts, setAlerts] = useState(false);
  const load = useCallback(() => {
    api
      .status()
      .then((s) => {
        setChecks(s.checks as Check[]);
        setAlerts(Boolean((s.alerts as { webhook?: boolean })?.webhook));
      })
      .catch(() => setChecks([]));
  }, []);
  useEffect(() => {
    load();
    const timer = setInterval(load, 15_000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <section className="space-y-2" aria-label="System status">
      <h3 className="font-medium">System status</h3>
      {!checks ? (
        <p className="text-muted">Checking…</p>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {checks.map((c) => (
            <li
              key={c.name}
              className="flex items-center gap-2 px-3 py-1.5"
              data-testid="status-check"
              data-level={c.level}
            >
              <span className={`h-2 w-2 shrink-0 rounded-full ${LEVEL_STYLE[c.level]}`} aria-label={c.level} />
              <span className="w-36 shrink-0">{NAMES[c.name] ?? c.name}</span>
              <span className="truncate text-muted">{c.summary}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted">
        {alerts
          ? "Problems are sent to your alert webhook."
          : "Set ALERT_WEBHOOK_URL (e.g. a Home Assistant webhook or an ntfy topic) to be told about problems."}
      </p>
    </section>
  );
}

const PUSH_TEXT: Record<PushState, string> = {
  unsupported:
    "This browser can't receive notifications. On iPhone, add Celius to the Home Screen first; over plain http (not https) they are unavailable.",
  denied: "Notifications are blocked for this site. Allow them in the browser's site settings.",
  off: "Get a notification on this device when a reply is ready or a tool needs your approval.",
  on: "On for this device. Nothing is shown for the chat you are looking at.",
};

function Notifications() {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    pushState()
      .then(setState)
      .catch(() => setState("off"));
  }, []);

  async function toggle() {
    setBusy(true);
    setMessage(null);
    try {
      setState(state === "on" ? await disablePush() : await enablePush());
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Could not change notifications");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-2" aria-label="Notifications">
      <h3 className="font-medium">Notifications</h3>
      <p className="text-xs text-muted" data-testid="push-state" data-state={state ?? "checking"}>
        {state ? PUSH_TEXT[state] : "Checking…"}
      </p>
      {(state === "on" || state === "off") && (
        <div className="flex items-center gap-2">
          <button className={button} disabled={busy} onClick={() => void toggle()}>
            {state === "on" ? "Turn off on this device" : "Turn on for this device"}
          </button>
          {state === "on" && (
            <button
              className={button}
              onClick={() =>
                void api
                  .pushTest()
                  .then(() => setMessage("Test sent."))
                  .catch(() => setMessage("Could not send a test"))
              }
            >
              Send a test
            </button>
          )}
          {message && (
            <span role="status" className="text-xs">
              {message}
            </span>
          )}
        </div>
      )}
    </section>
  );
}

function PasswordForm() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setMessage(null);
    if (next !== again) {
      setError("The new passwords don't match");
      return;
    }
    try {
      await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setAgain("");
      setMessage("Password changed. Your other devices have been signed out.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the password");
    }
  }

  return (
    <form className="space-y-2" onSubmit={submit} aria-label="Change password">
      <h3 className="font-medium">Change password</h3>
      <input
        className={input}
        type="password"
        autoComplete="current-password"
        placeholder="Current password"
        aria-label="Current password"
        value={current}
        onChange={(e) => setCurrent(e.target.value)}
      />
      <input
        className={input}
        type="password"
        autoComplete="new-password"
        placeholder="New password (12+ characters)"
        aria-label="New password"
        minLength={12}
        value={next}
        onChange={(e) => setNext(e.target.value)}
      />
      <input
        className={input}
        type="password"
        autoComplete="new-password"
        placeholder="New password again"
        aria-label="New password again"
        value={again}
        onChange={(e) => setAgain(e.target.value)}
      />
      <div className="flex items-center gap-2">
        <button className={button} disabled={!current || next.length < 12}>
          Change password
        </button>
        {message && (
          <span role="status" className="text-xs">
            {message}
          </span>
        )}
        {error && (
          <span role="alert" className="text-xs text-red-600 dark:text-red-400">
            {error}
          </span>
        )}
      </div>
    </form>
  );
}

interface SessionRow {
  id: string;
  user_agent: string | null;
  created_at: string;
  last_seen_at: string | null;
  current: boolean;
}

function device(agent: string | null) {
  if (!agent) return "Unknown device";
  if (/Tauri|Celius/i.test(agent)) return "Windows app";
  if (/Android/i.test(agent)) return "Android";
  if (/iPhone|iPad/i.test(agent)) return "iOS";
  if (/Windows/i.test(agent)) return "Windows browser";
  if (/Mac OS/i.test(agent)) return "Mac browser";
  if (/Linux/i.test(agent)) return "Linux browser";
  return agent.slice(0, 40);
}

function Sessions() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const load = useCallback(() => {
    api
      .sessions()
      .then((r) => setRows(r as SessionRow[]))
      .catch(() => setRows([]));
  }, []);
  useEffect(load, [load]);
  const others = rows?.filter((r) => !r.current).length ?? 0;
  return (
    <section className="space-y-2" aria-label="Signed-in devices">
      <h3 className="font-medium">Signed-in devices</h3>
      <ul className="space-y-1">
        {rows?.map((r) => (
          <li key={r.id} className="flex gap-2 text-xs">
            <span className="w-32 shrink-0">{device(r.user_agent)}</span>
            <span className="text-muted">
              since {new Date(r.created_at).toLocaleDateString()}
              {r.current ? " · this device" : ""}
            </span>
          </li>
        ))}
      </ul>
      <button className={button} disabled={!others} onClick={() => void api.revokeOtherSessions().then(load)}>
        Sign out other devices
      </button>
    </section>
  );
}
