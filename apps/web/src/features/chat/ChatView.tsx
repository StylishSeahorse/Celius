import { useEffect, useRef, useState } from "react";

import type { ModelList, StoredFile } from "../../lib/types";
import { outbox } from "../../offline/outbox";
import { Composer } from "./Composer";
import { runExport } from "./exporting";
import { MessageView, PendingMessage } from "./MessageView";
import type { Conflict, useChat } from "./useChat";

interface Props {
  chat: ReturnType<typeof useChat>;
  models: ModelList | null;
  online: boolean;
  draftModel: string | null;
  draftReasoning: boolean | null;
  projectName: string | null;
  extraAttach?: { label: string; pick: () => Promise<File[]> };
  onDraftModel: (model: string | null) => void;
  onDraftReasoning: (value: boolean | null) => void;
  onOpenSidebar: () => void;
  onStorageChanged: () => void;
}

export function ChatView(props: Props) {
  const { chat, models, online, draftModel, projectName, onDraftModel, onOpenSidebar } = props;
  const { detail, live } = chat;
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const [conflict, setConflict] = useState<Conflict | null>(null);

  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [detail?.messages.length, live?.text, live?.status, chat.pending.length]);

  const messages = detail?.messages ?? [];
  const canStop = live !== null && ["queued", "running", "reconnecting"].includes(live.status);
  const savedOnServer = detail !== null && detail.version > 0;
  const actions = {
    disabled: chat.busy || !online || !savedOnServer,
    onSelect: (id: string) => void chat.selectSibling(id),
    onRegenerate: (id: string) => void chat.regenerate(id),
    onEdit: (id: string, content: string) => void chat.edit(id, content),
  };

  async function save(fields: Record<string, unknown>, expectedVersion?: number) {
    const result = await chat.update(fields, expectedVersion);
    setConflict(result);
  }

  // Capabilities of the model this conversation will use.
  const modelId = (detail ? detail.model_override : draftModel) ?? models?.default_model_id;
  const model =
    models?.models.find((m) => m.id === modelId) ?? (models?.models.length === 1 ? models.models[0] : undefined);
  const reasoning = detail && savedOnServer ? detail.reasoning : props.draftReasoning;

  return (
    <main aria-label="Chat" className="flex h-full min-w-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-line px-4 py-2.5">
        <button
          onClick={onOpenSidebar}
          className="rounded-md px-2 py-1 text-muted hover:bg-panel md:hidden"
          aria-label="Open conversations"
        >
          ☰
        </button>
        <div className="min-w-0 flex-1">
          {projectName && <p className="truncate text-xs text-muted">{projectName}</p>}
          <Title
            title={detail?.title || "New chat"}
            editable={savedOnServer && online}
            onRename={(title) => void save({ title })}
          />
        </div>
        <ModelPicker
          models={models}
          value={detail ? detail.model_override : draftModel}
          onChange={(model) => (savedOnServer ? void save({ model_override: model }) : onDraftModel(model))}
        />
        {savedOnServer && online && <ExportMenu conversationId={detail!.id} />}
      </header>

      {(!online || chat.fromCache) && (
        <div role="status" className="border-b border-line bg-panel px-4 py-1.5 text-center text-xs text-muted">
          {online
            ? "Showing the copy saved on this device while the latest loads…"
            : "You're offline. Showing saved conversations; new messages will be sent when you reconnect."}
        </div>
      )}

      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="flex-1 overflow-y-auto"
      >
        <div className="mx-auto max-w-3xl space-y-6 px-4 py-6">
          {chat.loading && <p className="text-muted">Loading…</p>}
          {!chat.loading && messages.length === 0 && chat.pending.length === 0 && (
            <EmptyState models={models} />
          )}
          {messages.map((m) => (
            <MessageView key={m.id} message={m} live={live} actions={actions} />
          ))}
          {chat.pending.map((item) => (
            <PendingMessage
              key={item.client_message_id}
              item={item}
              online={online}
              onRetry={() => void outbox.retry(item.client_message_id)}
              onDiscard={() => void outbox.discard(item.client_message_id)}
            />
          ))}
        </div>
      </div>

      <div className="mx-auto w-full max-w-3xl px-4 pb-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {chat.error && (
          <p role="alert" className="mb-2 text-sm text-red-600 dark:text-red-400">
            {chat.error}
          </p>
        )}
        <Composer
          canStop={canStop}
          online={online}
          vision={model?.capabilities?.vision ?? false}
          reasoning={reasoning ?? null}
          reasoningSupported={model?.capabilities?.reasoning_toggle ?? false}
          extraAttach={props.extraAttach}
          onSend={(content, files: StoredFile[]) =>
            chat.send(content, files.map((f) => ({ id: f.id, filename: f.relative_path ?? f.filename })))
          }
          onStop={() => void chat.cancel()}
          onReasoning={(value) => (savedOnServer ? void save({ reasoning: value }) : props.onDraftReasoning(value))}
          onStorageChanged={props.onStorageChanged}
        />
      </div>

      {conflict && (
        <ConflictDialog
          conflict={conflict}
          onKeepTheirs={() => {
            chat.setDetail((current) => (current ? { ...current, ...conflict.current } : current));
            setConflict(null);
          }}
          onUseMine={() => void save(conflict.fields, conflict.current.version)}
        />
      )}
    </main>
  );
}

function ExportMenu({ conversationId }: { conversationId: string }) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  async function exportAs(format: "markdown" | "json" | "zip") {
    setOpen(false);
    setStatus("Preparing export…");
    try {
      await runExport({ scope: "conversation", id: conversationId, format });
      setStatus(null);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Export failed");
      setTimeout(() => setStatus(null), 4000);
    }
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="rounded-md border border-line px-2 py-1 text-sm hover:bg-panel"
      >
        {status ?? "Export"}
      </button>
      {open && (
        <div role="menu" className="absolute right-0 z-20 mt-1 w-48 rounded-lg border border-line bg-canvas p-1 shadow-lg">
          {(
            [
              ["markdown", "Markdown (.md)"],
              ["json", "JSON (all versions)"],
              ["zip", "ZIP with attachments"],
            ] as const
          ).map(([format, label]) => (
            <button
              key={format}
              role="menuitem"
              onClick={() => void exportAs(format)}
              className="block w-full rounded-md px-3 py-1.5 text-left text-sm hover:bg-panel"
            >
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Title({ title, editable, onRename }: { title: string; editable: boolean; onRename: (t: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);
  if (!editing) {
    return (
      <h1
        className={`truncate font-medium ${editable ? "cursor-text" : ""}`}
        title={editable ? "Click to rename" : undefined}
        onClick={() => {
          if (!editable) return;
          setValue(title);
          setEditing(true);
        }}
      >
        {title}
      </h1>
    );
  }
  const commit = () => {
    setEditing(false);
    const next = value.trim();
    if (next && next !== title) onRename(next);
  };
  return (
    <input
      autoFocus
      aria-label="Conversation title"
      value={value}
      maxLength={300}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") setEditing(false);
      }}
      className="w-full rounded border border-line bg-canvas px-1.5 py-0.5 font-medium outline-none focus:border-accent"
    />
  );
}

function ConflictDialog({
  conflict,
  onKeepTheirs,
  onUseMine,
}: {
  conflict: Conflict;
  onKeepTheirs: () => void;
  onUseMine: () => void;
}) {
  const theirs = conflict.current as unknown as Record<string, unknown>;
  const changed = Object.keys(conflict.fields);
  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="conflict-title" className="w-full max-w-md space-y-4 rounded-2xl bg-canvas p-6 shadow-xl">
        <h2 id="conflict-title" className="text-lg font-semibold">
          Changed on another device
        </h2>
        <p className="text-sm text-muted">
          This conversation was updated somewhere else after you opened it. Which version should be kept?
        </p>
        <dl className="space-y-2 rounded-lg border border-line p-3 text-sm">
          {changed.map((field) => (
            <div key={field} className="grid grid-cols-[6rem_1fr] gap-x-3">
              <dt className="text-muted">{field.replace("_", " ")}</dt>
              <dd>
                <span className="block">Theirs: {String(theirs[field] ?? "—")}</span>
                <span className="block">Yours: {String(conflict.fields[field] ?? "—")}</span>
              </dd>
            </div>
          ))}
        </dl>
        <div className="flex justify-end gap-2">
          <button onClick={onKeepTheirs} className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel">
            Keep theirs
          </button>
          <button onClick={onUseMine} className="rounded-lg bg-accent px-3 py-1.5 text-sm text-white hover:bg-accent-strong">
            Use mine
          </button>
        </div>
      </div>
    </div>
  );
}

function EmptyState({ models }: { models: ModelList | null }) {
  return (
    <div className="pt-[15vh] text-center">
      <p className="text-2xl font-medium">How can I help?</p>
      {models && !models.health.ok && (
        <p className="mt-3 text-sm text-muted">
          The model server is offline. Messages will be answered when it is back.
        </p>
      )}
    </div>
  );
}

function ModelPicker({
  models,
  value,
  onChange,
}: {
  models: ModelList | null;
  value: string | null;
  onChange: (model: string | null) => void;
}) {
  if (!models) return null;
  const ids = new Set(models.models.map((m) => m.id));
  if (value) ids.add(value);
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      aria-label="Model"
      className="max-w-[45%] truncate rounded-md border border-line bg-canvas px-2 py-1 text-sm"
    >
      <option value="">Default ({models.default_model_id})</option>
      {[...ids].map((id) => (
        <option key={id} value={id}>
          {id}
        </option>
      ))}
    </select>
  );
}
