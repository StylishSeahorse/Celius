import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, api, type TaskFields } from "../../lib/api";
import type { AssistantSettings, Briefing, UserTask } from "../../lib/types";

type Tab = "today" | "tasks" | "settings";

const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const small = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";
const input = "rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent";

const PRIORITY_LABEL: Record<string, string> = { low: "low", normal: "", high: "high", urgent: "urgent" };
const REPEATS = ["", "daily", "weekdays", "weekly", "monthly", "yearly"] as const;

function message(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

function dueLabel(task: UserTask) {
  if (!task.due_date) return null;
  if (task.due_all_day) return task.due_date;
  return task.due_at ? new Date(task.due_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : null;
}

/** The personal-assistant dashboard: today's briefing, the task list and settings. */
export function TodayPanel({
  onClose,
  onChanged,
  onOpenConversation,
  onOpenMemory,
  onOpenSkills,
}: {
  onClose: () => void;
  onChanged: () => void;
  onOpenConversation: (id: string) => void;
  onOpenMemory: () => void;
  onOpenSkills: () => void;
}) {
  const [tab, setTab] = useState<Tab>("today");
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  const reload = useCallback(() => {
    api.briefing().then(setBriefing, (err) => setError(message(err)));
  }, []);
  useEffect(reload, [reload, version]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    const onChange = () => setVersion((v) => v + 1);
    window.addEventListener("celius:user-tasks-changed", onChange);
    return () => window.removeEventListener("celius:user-tasks-changed", onChange);
  }, []);

  const changed = () => {
    setVersion((v) => v + 1);
    onChanged();
  };

  return (
    <div className="fixed inset-0 z-30 flex items-stretch justify-center bg-black/40 sm:items-center sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="today-title"
        className="flex h-full w-full max-w-2xl flex-col bg-canvas shadow-xl sm:h-[85vh] sm:rounded-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <h2 id="today-title" className="flex-1 font-semibold">
            Today
          </h2>
          <button onClick={onClose} className={button} aria-label="Close">
            ✕
          </button>
        </header>
        <nav className="flex gap-1 border-b border-line px-3 py-2" aria-label="Today sections">
          {(
            [
              ["today", briefing ? `Briefing · ${briefing.date}` : "Briefing"],
              ["tasks", "Tasks"],
              ["settings", "Settings"],
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              aria-current={tab === key ? "page" : undefined}
              className={`rounded-md px-3 py-1 text-sm ${tab === key ? "bg-panel font-medium" : "text-muted hover:text-ink"}`}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm">
          {error && <p role="alert" className="text-red-600 dark:text-red-400">{error}</p>}
          {tab === "today" && briefing && (
            <BriefingView
              briefing={briefing}
              onChanged={changed}
              onOpenConversation={onOpenConversation}
              onOpenMemory={onOpenMemory}
              onOpenSkills={onOpenSkills}
            />
          )}
          {tab === "tasks" && <TaskList key={version} onChanged={changed} />}
          {tab === "settings" && <Settings />}
        </div>
      </div>
    </div>
  );
}

function BriefingView({
  briefing,
  onChanged,
  onOpenConversation,
  onOpenMemory,
  onOpenSkills,
}: {
  briefing: Briefing;
  onChanged: () => void;
  onOpenConversation: (id: string) => void;
  onOpenMemory: () => void;
  onOpenSkills: () => void;
}) {
  const nothing =
    !briefing.overdue.length &&
    !briefing.due_today.length &&
    !briefing.plan.length &&
    !briefing.upcoming.length &&
    !briefing.suggestions.length;
  const w = briefing.waiting;
  return (
    <div className="space-y-4" data-testid="briefing">
      {nothing && <p className="py-4 text-center text-muted">Nothing due. Add tasks in the Tasks tab, or ask Celius.</p>}
      <Section title="Overdue" tasks={briefing.overdue} onChanged={onChanged} />
      <Section title="Due today" tasks={briefing.due_today} onChanged={onChanged} />
      <Section title="Suggested order" tasks={briefing.plan} onChanged={onChanged} numbered />
      <Section title="Next 7 days" tasks={briefing.upcoming} onChanged={onChanged} />
      {briefing.suggestions.length > 0 && (
        <section className="space-y-1" aria-label="Suggested tasks">
          <h3 className="font-medium">Noticed in your chats</h3>
          <ul className="space-y-1">
            {briefing.suggestions.map((t) => (
              <li key={t.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-line p-2" data-testid="suggestion">
                <span className="flex-1">
                  {t.title}
                  {t.due_date && <span className="text-xs text-muted"> · {t.due_date}</span>}
                </span>
                <button className={small} onClick={() => void api.acceptUserTask(t.id).then(onChanged)}>
                  Add to my tasks
                </button>
                <button className={small} onClick={() => void api.deleteUserTask(t.id).then(onChanged)}>
                  Dismiss
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      {briefing.agent_tasks.length > 0 && (
        <section className="space-y-1" aria-label="Celius's open work">
          <h3 className="font-medium">Celius's open work</h3>
          <ul className="space-y-1 text-xs">
            {briefing.agent_tasks.map((t) => (
              <li key={t.id}>
                <button className="underline" onClick={() => onOpenConversation(t.conversation_id)}>
                  {t.objective}
                </button>{" "}
                <span className="text-muted">({t.status})</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {(w.approvals > 0 || w.memories > 0 || w.skills > 0) && (
        <section className="space-y-1 text-xs" aria-label="Waiting for you">
          <h3 className="text-sm font-medium">Waiting for you</h3>
          {w.approvals > 0 && <p>{w.approvals} tool calls to approve (Tools & activity)</p>}
          {w.memories > 0 && (
            <button className="block underline" onClick={onOpenMemory}>
              {w.memories} memories to review
            </button>
          )}
          {w.skills > 0 && (
            <button className="block underline" onClick={onOpenSkills}>
              {w.skills} skills to review
            </button>
          )}
        </section>
      )}
      <p className="text-xs text-muted">
        Times are in {briefing.timezone}. The suggested order puts overdue and today's tasks first, then priority.
        Ask Celius to “plan my day” for a plan in words.
      </p>
    </div>
  );
}

function Section({
  title,
  tasks,
  onChanged,
  numbered,
}: {
  title: string;
  tasks: UserTask[];
  onChanged: () => void;
  numbered?: boolean;
}) {
  if (!tasks.length) return null;
  return (
    <section className="space-y-1" aria-label={title}>
      <h3 className="font-medium">{title}</h3>
      <ul className="space-y-1">
        {tasks.map((t, i) => (
          <li key={t.id} className="flex items-center gap-2" data-testid="briefing-task">
            {numbered && <span className="w-4 text-xs text-muted">{i + 1}.</span>}
            <input
              type="checkbox"
              aria-label={`Done: ${t.title}`}
              onChange={() => void api.updateUserTask(t.id, { status: "done" }).then(onChanged)}
            />
            <span className={t.overdue ? "text-red-600 dark:text-red-400" : ""}>{t.title}</span>
            <TaskMeta task={t} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function TaskMeta({ task }: { task: UserTask }) {
  const due = dueLabel(task);
  return (
    <span className="text-xs text-muted">
      {due && ` · ${due}`}
      {PRIORITY_LABEL[task.priority] && ` · ${PRIORITY_LABEL[task.priority]}`}
      {task.recurrence && ` · repeats ${task.recurrence.freq}`}
      {task.blocked && " · waiting on another task"}
      {task.status === "in_progress" && " · in progress"}
    </span>
  );
}

function TaskList({ onChanged }: { onChanged: () => void }) {
  const [filter, setFilter] = useState<"active" | "done" | "all">("active");
  const [items, setItems] = useState<UserTask[] | null>(null);
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [priority, setPriority] = useState<TaskFields["priority"]>("normal");
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(() => {
    api.userTasks(filter).then(setItems, (err) => setError(message(err)));
  }, [filter]);
  useEffect(load, [load]);
  const act = (p: Promise<unknown>) =>
    void p.then(
      () => {
        setError(null);
        load();
        onChanged();
      },
      (err) => setError(message(err)),
    );

  function add(event: FormEvent) {
    event.preventDefault();
    if (!title.trim()) return;
    act(
      api.createUserTask({ title, due: due || undefined, priority }).then(() => {
        setTitle("");
        setDue("");
        setPriority("normal");
      }),
    );
  }

  const top = (items ?? []).filter((t) => !t.parent_id || !items?.some((p) => p.id === t.parent_id));
  const children = (id: string) => (items ?? []).filter((t) => t.parent_id === id);

  return (
    <div className="space-y-3">
      <form onSubmit={add} className="flex flex-wrap gap-2 rounded-lg border border-dashed border-line p-2">
        <input
          className={`${input} min-w-0 flex-1`}
          placeholder="Add a task"
          aria-label="New task"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <input type="date" className={input} aria-label="Due date" value={due} onChange={(e) => setDue(e.target.value)} />
        <select
          className={input}
          aria-label="Priority"
          value={priority}
          onChange={(e) => setPriority(e.target.value as TaskFields["priority"])}
        >
          <option value="low">Low</option>
          <option value="normal">Normal</option>
          <option value="high">High</option>
          <option value="urgent">Urgent</option>
        </select>
        <button className={primary} disabled={!title.trim()}>
          Add
        </button>
      </form>
      <div className="flex gap-1">
        {(["active", "done", "all"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            aria-pressed={filter === f}
            className={`rounded-md px-2 py-0.5 text-xs ${filter === f ? "bg-panel font-medium" : "text-muted"}`}
          >
            {f === "active" ? "To do" : f === "done" ? "Done" : "All"}
          </button>
        ))}
      </div>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {items && !items.length && <p className="py-4 text-center text-muted">No tasks here.</p>}
      <ul className="space-y-1">
        {top.map((t) => (
          <TaskRow
            key={t.id}
            task={t}
            subtasks={children(t.id)}
            open={open === t.id}
            onToggle={() => setOpen(open === t.id ? null : t.id)}
            act={act}
          />
        ))}
      </ul>
    </div>
  );
}

function TaskRow({
  task,
  subtasks,
  open,
  onToggle,
  act,
}: {
  task: UserTask;
  subtasks: UserTask[];
  open: boolean;
  onToggle: () => void;
  act: (p: Promise<unknown>) => void;
}) {
  const done = task.status === "done";
  return (
    <li className="rounded-lg border border-line p-2" data-testid="user-task">
      <div className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={done}
          aria-label={`Done: ${task.title}`}
          onChange={() => act(api.updateUserTask(task.id, { status: done ? "open" : "done" }))}
        />
        <button className={`min-w-0 flex-1 truncate text-left ${done ? "text-muted line-through" : ""}`} onClick={onToggle}>
          <span className={task.overdue ? "text-red-600 dark:text-red-400" : ""}>{task.title}</span>
          <TaskMeta task={task} />
        </button>
      </div>
      {subtasks.length > 0 && (
        <ul className="ml-6 mt-1 space-y-0.5">
          {subtasks.map((s) => (
            <li key={s.id} className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={s.status === "done"}
                aria-label={`Done: ${s.title}`}
                onChange={() => act(api.updateUserTask(s.id, { status: s.status === "done" ? "open" : "done" }))}
              />
              <span className={s.status === "done" ? "text-muted line-through" : ""}>{s.title}</span>
            </li>
          ))}
        </ul>
      )}
      {open && <TaskEditor task={task} act={act} />}
    </li>
  );
}

function TaskEditor({ task, act }: { task: UserTask; act: (p: Promise<unknown>) => void }) {
  const [title, setTitle] = useState(task.title);
  const [notes, setNotes] = useState(task.notes ?? "");
  const [due, setDue] = useState(task.due_all_day ? (task.due_date ?? "") : "");
  const [remind, setRemind] = useState("");
  const [repeat, setRepeat] = useState(task.recurrence?.freq ?? "");
  const [subtask, setSubtask] = useState("");
  return (
    <div className="mt-2 space-y-2 border-t border-line pt-2 text-xs">
      <input className={`${input} w-full`} aria-label="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
      <textarea
        className={`${input} w-full`}
        rows={2}
        aria-label="Notes"
        placeholder="Notes"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
      />
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1">
          Due
          <input type="date" className={input} aria-label="Edit due date" value={due} onChange={(e) => setDue(e.target.value)} />
        </label>
        <label className="flex items-center gap-1">
          Remind me
          <input type="datetime-local" className={input} aria-label="Reminder" value={remind} onChange={(e) => setRemind(e.target.value)} />
        </label>
        <label className="flex items-center gap-1">
          Repeat
          <select className={input} aria-label="Repeat" value={repeat} onChange={(e) => setRepeat(e.target.value)}>
            {REPEATS.map((r) => (
              <option key={r} value={r}>
                {r || "never"}
              </option>
            ))}
          </select>
        </label>
      </div>
      {task.remind_at && (
        <p className="text-muted">
          Reminder set for {new Date(task.remind_at).toLocaleString()}
          {task.reminded_at ? " (sent)" : ""}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          className={small}
          onClick={() =>
            act(
              api.updateUserTask(task.id, {
                title,
                notes: notes || null,
                due: task.due_all_day || due ? due : undefined,
                ...(remind ? { remind } : {}),
                ...(repeat ? { recurrence: { freq: repeat as never } } : { clear_recurrence: true }),
              }),
            )
          }
        >
          Save
        </button>
        <button className={small} onClick={() => act(api.updateUserTask(task.id, { status: "in_progress" }))}>
          Start
        </button>
        <button className={small} onClick={() => act(api.updateUserTask(task.id, { status: "cancelled" }))}>
          Cancel task
        </button>
        <button className={small} onClick={() => confirm(`Delete “${task.title}”?`) && act(api.deleteUserTask(task.id))}>
          Delete
        </button>
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (subtask.trim()) act(api.createUserTask({ title: subtask, parent_id: task.id }).then(() => setSubtask("")));
        }}
      >
        <input
          className={`${input} flex-1`}
          placeholder="Add a subtask"
          aria-label="New subtask"
          value={subtask}
          onChange={(e) => setSubtask(e.target.value)}
        />
        <button className={small} disabled={!subtask.trim()}>
          Add
        </button>
      </form>
    </div>
  );
}

function Settings() {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [briefingTime, setBriefingTime] = useState("");
  useEffect(() => {
    api.assistantSettings().then((s) => {
      setSettings(s);
      setBriefingTime(s.briefing_time ?? "");
    }, (err) => setError(message(err)));
  }, []);
  const save = (body: { timezone?: string; suggest_tasks?: boolean; briefing_time?: string }) =>
    void api.updateAssistantSettings(body).then(setSettings, (err) => setError(message(err)));
  if (!settings) return error ? <p role="alert">{error}</p> : null;
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <div className="space-y-4">
      <label className="block space-y-1">
        <span>Time zone</span>
        <input
          className={`${input} w-full`}
          aria-label="Time zone"
          defaultValue={settings.timezone}
          onBlur={(e) => e.target.value !== settings.timezone && save({ timezone: e.target.value })}
        />
        {browserZone && browserZone !== settings.timezone && (
          <button className={small} onClick={() => save({ timezone: browserZone })}>
            Use this device's time zone ({browserZone})
          </button>
        )}
      </label>
      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          className="mt-1"
          checked={settings.suggest_tasks}
          onChange={(e) => save({ suggest_tasks: e.target.checked })}
        />
        <span>
          Notice to-dos in my chats
          <span className="block text-xs text-muted">
            When you mention something you need to do, Celius suggests it here. Nothing is added until you accept it.
            Needs memory suggestions to be on.
          </span>
        </span>
      </label>
      <div className="space-y-1">
        <span>Daily briefing</span>
        <div className="flex items-center gap-2">
          <input
            type="time"
            className={input}
            aria-label="Briefing time"
            value={briefingTime}
            onChange={(e) => setBriefingTime(e.target.value)}
          />
          <button className={small} onClick={() => save({ briefing_time: briefingTime })} disabled={!briefingTime}>
            {settings.briefing_time ? "Change" : "Turn on"}
          </button>
          {settings.briefing_time && (
            <button className={small} onClick={() => (setBriefingTime(""), save({ briefing_time: "" }))}>
              Turn off
            </button>
          )}
        </div>
        <p className="text-xs text-muted">
          Each day at this time Celius writes a short plan in a new chat (read-only: it never changes your tasks). It
          appears with your scheduled tasks.
        </p>
      </div>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}
