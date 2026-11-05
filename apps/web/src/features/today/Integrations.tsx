import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { Automation, AutomationRun, CalendarEventList, IntegrationAccount } from "../../lib/types";

const small = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";
const input = "rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent";

function message(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

// ---------------------------------------------------------------------- calendar

export function CalendarView() {
  const [data, setData] = useState<CalendarEventList | null>(null);
  const [accounts, setAccounts] = useState<IntegrationAccount[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [calendar, setCalendar] = useState("");

  const load = useCallback(() => {
    api.calendarEvents(undefined, 7).then(setData, (err) => setError(message(err)));
    api.accounts().then((a) => setAccounts(a.filter((x) => x.kind === "calendar")), () => undefined);
  }, []);
  useEffect(load, [load]);

  function add(event: FormEvent) {
    event.preventDefault();
    api
      .createEvent({ title, start, end: end || undefined, calendar: calendar || null })
      .then(() => {
        setTitle("");
        setStart("");
        setEnd("");
        load();
      }, (err) => setError(message(err)));
  }

  const clashing = new Set((data?.conflicts ?? []).flat());
  const days = new Map<string, CalendarEventList["events"]>();
  for (const e of data?.events ?? []) {
    const day = new Date(e.start).toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
    days.set(day, [...(days.get(day) ?? []), e]);
  }

  return (
    <div className="space-y-3">
      <form onSubmit={add} className="flex flex-wrap gap-2 rounded-lg border border-dashed border-line p-2">
        <input className={`${input} min-w-0 flex-1`} placeholder="New event" aria-label="Event title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <input type="datetime-local" className={input} aria-label="Starts" value={start} onChange={(e) => setStart(e.target.value)} />
        <input type="datetime-local" className={input} aria-label="Ends" value={end} onChange={(e) => setEnd(e.target.value)} />
        {accounts.length > 0 && (
          <select className={input} aria-label="Calendar" value={calendar} onChange={(e) => setCalendar(e.target.value)}>
            <option value="">Celius</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        )}
        <button className={primary} disabled={!title.trim() || !start}>
          Add
        </button>
      </form>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {data?.problems.map((p) => (
        <p key={p} className="text-xs text-amber-700 dark:text-amber-300">
          {p}
        </p>
      ))}
      {data && !data.events.length && <p className="py-4 text-center text-muted">Nothing in the next 7 days.</p>}
      {[...days.entries()].map(([day, events]) => (
        <section key={day} className="space-y-1" aria-label={day}>
          <h3 className="font-medium">{day}</h3>
          <ul className="space-y-1">
            {events.map((e) => (
              <li key={e.id} className="flex items-center gap-2 text-sm" data-testid="calendar-event">
                <span className="w-24 shrink-0 text-xs text-muted">
                  {e.all_day
                    ? "all day"
                    : `${new Date(e.start).toLocaleTimeString([], { timeStyle: "short" })}–${new Date(e.end).toLocaleTimeString([], { timeStyle: "short" })}`}
                </span>
                <span className={`flex-1 ${clashing.has(e.id) ? "text-amber-700 dark:text-amber-300" : ""}`}>
                  {e.title}
                  {e.location && <span className="text-xs text-muted"> · {e.location}</span>}
                  {e.calendar !== "Celius" && <span className="text-xs text-muted"> · {e.calendar}</span>}
                  {clashing.has(e.id) && <span className="text-xs"> · overlaps</span>}
                </span>
                {e.editable && (
                  <button className={small} aria-label={`Delete ${e.title}`} onClick={() => confirm(`Delete “${e.title}”?`) && void api.deleteEvent(e.id).then(load, (err) => setError(message(err)))}>
                    ✕
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------- automations

type Recipe = { label: string; build: (accountId?: string) => { name: string; trigger: object; conditions?: object[]; actions: object[] } };

const RECIPES: Record<string, Recipe> = {
  overdue: {
    label: "Tell me when a task becomes overdue",
    build: () => ({
      name: "Overdue tasks",
      trigger: { type: "task_overdue" },
      actions: [{ type: "notify", title: "Overdue", body: "{task.title} was due {task.due}" }],
    }),
  },
  due_soon: {
    label: "Remind me a day before a deadline",
    build: () => ({
      name: "Deadlines tomorrow",
      trigger: { type: "task_due_soon", hours: 24 },
      actions: [{ type: "notify", title: "Due soon", body: "{task.title} is due {task.due}" }],
    }),
  },
  briefing: {
    label: "Every morning at 07:30, prepare a briefing",
    build: () => ({
      name: "Morning briefing",
      trigger: { type: "schedule", kind: "daily", time: "07:30" },
      actions: [{ type: "run_prompt", prompt: "Prepare my daily briefing: call daily_briefing and suggest a plan.", tool_profile: "read_only" }],
    }),
  },
  email_task: {
    label: "When an email mentions an invoice, add a task",
    build: (accountId) => ({
      name: "Invoices to tasks",
      trigger: { type: "email_received", account_id: accountId },
      conditions: [{ field: "email.subject", op: "contains", value: "invoice" }],
      actions: [{ type: "create_task", title: "Pay: {email.subject}", priority: "high", due_in_days: 7 }],
    }),
  },
  page: {
    label: "Tell me when a web page changes",
    build: () => ({
      name: "Page watch",
      trigger: { type: "web_page_changed", url: "https://", interval_minutes: 60 },
      actions: [{ type: "notify", title: "Page changed", body: "{page.url}: {page.change}" }],
    }),
  },
};

function describeTrigger(t: Record<string, unknown>) {
  switch (t.type) {
    case "schedule":
      return t.kind === "interval" ? `every ${t.interval_minutes} minutes` : `${t.kind} at ${t.time}`;
    case "task_overdue":
      return "a task becomes overdue";
    case "task_due_soon":
      return `a deadline is within ${t.hours} hours`;
    case "task_status":
      return `a task is marked ${t.status}`;
    case "task_created":
      return "a task is added";
    case "email_received":
      return "an email arrives";
    case "web_page_changed":
      return `${t.url} changes`;
    default:
      return String(t.type);
  }
}

export function AutomationsView() {
  const [items, setItems] = useState<Automation[] | null>(null);
  const [emailAccounts, setEmailAccounts] = useState<IntegrationAccount[]>([]);
  const [recipe, setRecipe] = useState("overdue");
  const [draft, setDraft] = useState(() => JSON.stringify(RECIPES.overdue.build(), null, 2));
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<{ id: string; runs: AutomationRun[] } | null>(null);

  const load = useCallback(() => {
    api.automations().then(setItems, (err) => setError(message(err)));
    api.accounts().then((a) => setEmailAccounts(a.filter((x) => x.kind === "email")), () => undefined);
  }, []);
  useEffect(load, [load]);

  function pick(key: string) {
    setRecipe(key);
    setDraft(JSON.stringify(RECIPES[key].build(emailAccounts[0]?.id), null, 2));
  }

  function create() {
    let body;
    try {
      body = JSON.parse(draft);
    } catch {
      setError("The definition isn't valid JSON");
      return;
    }
    api.createAutomation(body).then(() => (setError(null), load()), (err) => setError(message(err)));
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">
        Rules that run on their own, even when no model is busy. You can also ask Celius in a chat, e.g. “tell me
        when a task becomes overdue”.
      </p>
      <ul className="space-y-1">
        {items?.map((a) => (
          <li key={a.id} className="space-y-1 rounded-lg border border-line p-2" data-testid="automation">
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={a.enabled}
                aria-label={`On: ${a.name}`}
                onChange={(e) => {
                  const enabled = e.target.checked;
                  // Show the change at once; the server's answer replaces it.
                  setItems((all) => all?.map((x) => (x.id === a.id ? { ...x, enabled } : x)) ?? null);
                  void api.updateAutomation(a.id, { enabled }).then(load, (err) => (setError(message(err)), load()));
                }}
              />
              <span className="flex-1">
                {a.name}
                <span className="block text-xs text-muted">
                  When {describeTrigger(a.trigger)} → {(a.actions as { type: string }[]).map((x) => x.type.replace("_", " ")).join(", ")}
                  {a.last_run_at && ` · last ${new Date(a.last_run_at).toLocaleString()} (${a.last_status})`}
                </span>
                {a.disabled_reason && <span className="block text-xs text-red-600 dark:text-red-400">{a.disabled_reason}</span>}
              </span>
              <button
                className={small}
                onClick={() =>
                  history?.id === a.id
                    ? setHistory(null)
                    : void api.automationRuns(a.id).then((runs) => setHistory({ id: a.id, runs }))
                }
              >
                History
              </button>
              <button className={small} aria-label={`Delete ${a.name}`} onClick={() => confirm(`Delete “${a.name}”?`) && void api.deleteAutomation(a.id).then(load)}>
                ✕
              </button>
            </div>
            {history?.id === a.id && (
              <ul className="ml-6 space-y-0.5 text-xs text-muted">
                {!history.runs.length && <li>Hasn't run yet.</li>}
                {history.runs.map((r) => (
                  <li key={r.id}>
                    {new Date(r.created_at).toLocaleString()} · {r.status} · {r.reason}
                    {r.error && <span className="text-red-600 dark:text-red-400"> · {r.error}</span>}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
      {items && !items.length && <p className="text-center text-muted">No automations yet.</p>}
      <section className="space-y-2 rounded-lg border border-dashed border-line p-2" aria-label="New automation">
        <select className={`${input} w-full`} aria-label="Start from" value={recipe} onChange={(e) => pick(e.target.value)}>
          {Object.entries(RECIPES).map(([key, r]) => (
            <option key={key} value={key} disabled={key === "email_task" && !emailAccounts.length}>
              {r.label}
              {key === "email_task" && !emailAccounts.length ? " (connect an email account first)" : ""}
            </option>
          ))}
        </select>
        <textarea className={`${input} w-full font-mono text-xs`} rows={8} aria-label="Definition" value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button className={primary} onClick={create}>
          Create
        </button>
      </section>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------- accounts

export function AccountsView() {
  const [items, setItems] = useState<IntegrationAccount[] | null>(null);
  const [kind, setKind] = useState<"calendar" | "email">("calendar");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<Record<string, string>>({});

  const load = useCallback(() => {
    api.accounts().then(setItems, (err) => setError(message(err)));
  }, []);
  useEffect(load, [load]);
  const set = (key: string) => (e: { target: { value: string } }) => setFields((f) => ({ ...f, [key]: e.target.value }));

  function add(event: FormEvent) {
    event.preventDefault();
    const config =
      kind === "calendar"
        ? { url: fields.url, username: fields.username }
        : {
            imap_host: fields.imap_host,
            smtp_host: fields.smtp_host,
            username: fields.username,
            from_address: fields.from_address || fields.username,
          };
    api
      .createAccount({
        kind,
        provider: kind === "calendar" ? "caldav" : "imap",
        name: fields.name || (kind === "calendar" ? "Calendar" : "Email"),
        config,
        password: fields.password,
      })
      .then(() => {
        setFields({});
        setError(null);
        load();
      }, (err) => setError(message(err)));
  }

  return (
    <div className="space-y-3">
      <h3 className="font-medium">Calendar and email</h3>
      <ul className="space-y-1">
        {items?.map((a) => (
          <li key={a.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-line p-2 text-sm" data-testid="account">
            <span className="flex-1">
              {a.name} <span className="text-xs text-muted">({a.kind === "calendar" ? "CalDAV calendar" : "IMAP/SMTP email"})</span>
              {(status[a.id] || a.last_error) && <span className="block text-xs text-muted">{status[a.id] ?? a.last_error}</span>}
            </span>
            <button
              className={small}
              onClick={() =>
                void api.checkAccount(a.id).then((r) => setStatus((s) => ({ ...s, [a.id]: r.ok ? "Connected" : (r.error ?? "Failed") })))
              }
            >
              Check
            </button>
            <button className={small} aria-label={`Remove ${a.name}`} onClick={() => confirm(`Remove ${a.name}?`) && void api.deleteAccount(a.id).then(load)}>
              ✕
            </button>
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="space-y-2 rounded-lg border border-dashed border-line p-2">
        <select className={input} aria-label="Account type" value={kind} onChange={(e) => setKind(e.target.value as "calendar" | "email")}>
          <option value="calendar">Calendar (CalDAV: Nextcloud, iCloud, Fastmail…)</option>
          <option value="email">Email (IMAP/SMTP: Gmail, Outlook… with an app password)</option>
        </select>
        <input className={`${input} w-full`} placeholder="Name" aria-label="Account name" value={fields.name ?? ""} onChange={set("name")} />
        {kind === "calendar" ? (
          <input className={`${input} w-full`} placeholder="Calendar URL" aria-label="Calendar URL" value={fields.url ?? ""} onChange={set("url")} />
        ) : (
          <>
            <input className={`${input} w-full`} placeholder="IMAP server, e.g. imap.gmail.com" aria-label="IMAP server" value={fields.imap_host ?? ""} onChange={set("imap_host")} />
            <input className={`${input} w-full`} placeholder="SMTP server, e.g. smtp.gmail.com" aria-label="SMTP server" value={fields.smtp_host ?? ""} onChange={set("smtp_host")} />
            <input className={`${input} w-full`} placeholder="Your address" aria-label="From address" value={fields.from_address ?? ""} onChange={set("from_address")} />
          </>
        )}
        <input className={`${input} w-full`} placeholder="User name" aria-label="User name" value={fields.username ?? ""} onChange={set("username")} />
        <input type="password" className={`${input} w-full`} placeholder="Password or app password" aria-label="Password" value={fields.password ?? ""} onChange={set("password")} />
        <button className={primary}>Connect</button>
        <p className="text-xs text-muted">
          The password is encrypted on the server and never shown again. Celius reads mail without marking it read,
          saves drafts to your Drafts folder, and asks you before sending every email.
        </p>
      </form>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}
