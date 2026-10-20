import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { Memory, MemorySettings, Project } from "../../lib/types";

type Tab = "review" | "remembered" | "settings";

const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";
const input = "w-full rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent";

function message(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

export function MemoryPanel({
  projects,
  initialTab,
  onClose,
  onChanged,
  onOpenConversation,
}: {
  projects: Project[];
  initialTab?: Tab;
  onClose: () => void;
  onChanged: () => void;
  onOpenConversation: (id: string) => void;
}) {
  const [tab, setTab] = useState<Tab>(initialTab ?? "remembered");
  const [settings, setSettings] = useState<MemorySettings | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reloadSettings = useCallback(() => {
    api.memorySettings().then(setSettings, (err) => setError(message(err)));
  }, []);
  useEffect(reloadSettings, [reloadSettings]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const changed = () => {
    reloadSettings();
    onChanged();
  };
  const proposed = settings?.counts?.proposed ?? 0;

  return (
    <div className="fixed inset-0 z-30 flex items-stretch justify-center bg-black/40 sm:items-center sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-title"
        className="flex h-full w-full max-w-2xl flex-col bg-canvas shadow-xl sm:h-[85vh] sm:rounded-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <h2 id="memory-title" className="flex-1 font-semibold">
            Memory
          </h2>
          <button onClick={onClose} className={button} aria-label="Close">
            ✕
          </button>
        </header>
        <nav className="flex gap-1 border-b border-line px-3 py-2" aria-label="Memory sections">
          {(
            [
              ["review", `To review${proposed ? ` (${proposed})` : ""}`],
              ["remembered", "Remembered"],
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
        <div className="flex-1 space-y-3 overflow-y-auto p-4 text-sm">
          {error && <p role="alert" className="text-red-600 dark:text-red-400">{error}</p>}
          {settings && !settings.enabled && tab !== "settings" && (
            <p className="rounded-lg bg-panel p-2 text-xs text-muted">
              Memory is off: nothing is remembered or used. Turn it on in Settings.
            </p>
          )}
          {tab === "review" && (
            <MemoryList status="proposed" projects={projects} onChanged={changed} onOpenConversation={onOpenConversation} />
          )}
          {tab === "remembered" && (
            <>
              <AddMemory projects={projects} onAdded={changed} />
              <MemoryList status="active" projects={projects} onChanged={changed} onOpenConversation={onOpenConversation} />
            </>
          )}
          {tab === "settings" && settings && (
            <Settings settings={settings} onSaved={setSettings} onForgotten={changed} />
          )}
        </div>
      </div>
    </div>
  );
}

function scopeLabel(memory: Memory, projects: Project[]) {
  if (memory.scope === "global") return "All chats";
  return `Project: ${projects.find((p) => p.id === memory.project_id)?.name ?? "deleted"}`;
}

function MemoryList({
  status,
  projects,
  onChanged,
  onOpenConversation,
}: {
  status: "active" | "proposed";
  projects: Project[];
  onChanged: () => void;
  onOpenConversation: (id: string) => void;
}) {
  const [items, setItems] = useState<Memory[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    api.memories(status, query || undefined).then(setItems, (err) => setError(message(err)));
  }, [status, query]);
  useEffect(() => {
    const timer = setTimeout(reload, query ? 250 : 0);
    return () => clearTimeout(timer);
  }, [reload, query]);
  const act = (p: Promise<unknown>) =>
    void p.then(
      () => {
        reload();
        onChanged();
      },
      (err) => setError(message(err)),
    );

  return (
    <div className="space-y-2">
      {status === "active" && (
        <input
          type="search"
          className={input}
          placeholder="Search memories"
          aria-label="Search memories"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      )}
      {error && <p role="alert" className="text-red-600 dark:text-red-400">{error}</p>}
      {items && !items.length && (
        <p className="py-6 text-center text-muted">
          {status === "proposed"
            ? "Nothing to review. Things Celius suggests remembering appear here first."
            : query
              ? "No matches."
              : "Nothing remembered yet."}
        </p>
      )}
      <ul className="space-y-2">
        {items?.map((m) => (
          <MemoryItem
            key={m.id}
            memory={m}
            scope={scopeLabel(m, projects)}
            onOpenConversation={onOpenConversation}
            onApprove={status === "proposed" ? (content) => act(api.approveMemory(m.id, content)) : undefined}
            onReject={status === "proposed" ? () => act(api.rejectMemory(m.id)) : undefined}
            onSave={status === "active" ? (content) => act(api.updateMemory(m.id, { content })) : undefined}
            onDelete={status === "active" ? () => act(api.deleteMemory(m.id)) : undefined}
          />
        ))}
      </ul>
    </div>
  );
}

function MemoryItem({
  memory,
  scope,
  onApprove,
  onReject,
  onSave,
  onDelete,
  onOpenConversation,
}: {
  memory: Memory;
  scope: string;
  onApprove?: (content?: string) => void;
  onReject?: () => void;
  onSave?: (content: string) => void;
  onDelete?: () => void;
  onOpenConversation: (id: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(memory.content);
  const source =
    memory.source === "user" ? "Added by you" : memory.source === "model" ? "Suggested in a chat" : "Noticed in a chat";

  return (
    <li className="space-y-1 rounded-lg border border-line p-3" data-testid="memory">
      {editing ? (
        <textarea className={input} rows={2} value={text} onChange={(e) => setText(e.target.value)} aria-label="Memory text" />
      ) : (
        <p>{memory.content}</p>
      )}
      <p className="text-xs text-muted">
        {scope} · {source}
        {memory.source_conversation_id && (
          <>
            {" "}
            <button className="underline" onClick={() => onOpenConversation(memory.source_conversation_id!)}>
              {memory.source_conversation_title ?? "chat"}
            </button>
          </>
        )}
        {memory.use_count ? ` · used ${memory.use_count}×` : ""}
      </p>
      <div className="flex flex-wrap gap-2">
        {onApprove && (
          <button className={primary} onClick={() => onApprove(editing && text !== memory.content ? text : undefined)}>
            {editing ? "Save & remember" : "Remember"}
          </button>
        )}
        {onSave && editing && (
          <button className={primary} onClick={() => (setEditing(false), onSave(text))} disabled={text.trim().length < 3}>
            Save
          </button>
        )}
        <button className={button} onClick={() => setEditing((e) => !e)}>
          {editing ? "Cancel" : "Edit"}
        </button>
        {onReject && (
          <button className={button} onClick={onReject}>
            Don't remember
          </button>
        )}
        {onDelete && (
          <button className={button} onClick={() => confirm("Delete this memory? It won't be used again.") && onDelete()}>
            Delete
          </button>
        )}
      </div>
    </li>
  );
}

function AddMemory({ projects, onAdded }: { projects: Project[]; onAdded: () => void }) {
  const [text, setText] = useState("");
  const [scope, setScope] = useState("global");
  const [error, setError] = useState<string | null>(null);
  async function add() {
    setError(null);
    try {
      await api.createMemory(
        scope === "global" ? { content: text, scope: "global" } : { content: text, scope: "project", project_id: scope },
      );
      setText("");
      onAdded();
    } catch (err) {
      setError(message(err));
    }
  }
  return (
    <div className="space-y-2 rounded-lg border border-dashed border-line p-3">
      <input
        className={input}
        placeholder="Add something to remember, e.g. I prefer metric units"
        aria-label="New memory"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && text.trim().length >= 3 && void add()}
      />
      <div className="flex items-center gap-2">
        <select className="rounded-md border border-line bg-canvas px-2 py-1 text-sm" value={scope} onChange={(e) => setScope(e.target.value)} aria-label="Remember for">
          <option value="global">For all chats</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              Only in {p.name}
            </option>
          ))}
        </select>
        <span className="flex-1" />
        <button className={primary} disabled={text.trim().length < 3} onClick={() => void add()}>
          Add
        </button>
      </div>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}

function Settings({
  settings,
  onSaved,
  onForgotten,
}: {
  settings: MemorySettings;
  onSaved: (s: MemorySettings) => void;
  onForgotten: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const save = (body: Partial<Pick<MemorySettings, "enabled" | "auto_extract" | "require_review">>) =>
    void api.updateMemorySettings(body).then(onSaved, (err) => setError(message(err)));
  const toggle = (key: "enabled" | "auto_extract" | "require_review", label: string, help: string) => (
    <label className="flex items-start gap-2">
      <input type="checkbox" className="mt-1" checked={settings[key]} onChange={(e) => save({ [key]: e.target.checked })} />
      <span>
        {label}
        <span className="block text-xs text-muted">{help}</span>
      </span>
    </label>
  );

  return (
    <div className="space-y-4">
      {toggle("enabled", "Use memory", "Celius remembers what you approve and recalls your earlier chats. Off: nothing is remembered or used.")}
      {toggle("auto_extract", "Suggest things to remember", "After each reply the model looks for lasting facts about you (uses some model time).")}
      {toggle("require_review", "Ask me before remembering", "Suggestions wait in To review until you approve them.")}
      <p className="text-xs text-muted">
        {settings.counts?.active ?? 0} remembered · {settings.counts?.proposed ?? 0} to review · {settings.counts?.excerpts ?? 0} chat
        excerpts for recall · search: {settings.embeddings ? "by meaning and words" : "by words (no embedding model configured)"}
      </p>
      <p className="text-xs text-muted">
        Recall stays within a project: chats in a project only recall that project's chats, and chats outside
        projects only recall other chats outside projects. Turn memory off for a single chat from its header,
        or for a project in its settings.
      </p>
      <section className="space-y-2 rounded-lg border border-line p-3">
        <h3 className="font-medium">What deleting does</h3>
        <p className="text-xs text-muted">
          Deleting a memory, an excerpt or a chat's memory removes it from Celius at once, with its search
          index: no later reply can use it. It does not change replies already written that used it, exports
          you downloaded, or backups made before (they age out with backup retention). The audit log keeps a
          record that something was deleted, not what it said. Your local model is never trained on your data.
        </p>
        <button
          className={button}
          onClick={() => {
            if (!confirm("Forget everything Celius remembers, including recall of earlier chats? Chats themselves stay.")) return;
            void api.forgetEverything().then(
              (r) => {
                setDone(`Forgot ${r.memories} memories and ${r.excerpts} chat excerpts.`);
                onForgotten();
              },
              (err) => setError(message(err)),
            );
          }}
        >
          Forget everything
        </button>
        {done && <p role="status" className="text-xs">{done}</p>}
      </section>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}
