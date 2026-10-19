import { useEffect, useRef } from "react";

import type { ConversationDetail, ModelList } from "../../lib/types";
import { Composer } from "./Composer";
import { MessageView } from "./MessageView";
import type { useChat } from "./useChat";

interface Props {
  chat: ReturnType<typeof useChat>;
  models: ModelList | null;
  modelOverride: string | null;
  onModelChange: (model: string | null) => void;
  onOpenSidebar: () => void;
}

export function ChatView({ chat, models, modelOverride, onModelChange, onOpenSidebar }: Props) {
  const { detail, live } = chat;
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [detail?.messages.length, live?.text, live?.status]);

  const messages = detail?.messages ?? [];
  const canStop = live !== null && ["queued", "running", "reconnecting"].includes(live.status);

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
        <h1 className="min-w-0 flex-1 truncate font-medium">{detail?.title || "New chat"}</h1>
        <ModelPicker models={models} value={modelOverride} onChange={onModelChange} />
      </header>

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
          {!chat.loading && messages.length === 0 && <EmptyState detail={detail} models={models} />}
          {messages.map((m) => (
            <MessageView key={m.id} message={m} live={live} />
          ))}
        </div>
      </div>

      <div className="mx-auto w-full max-w-3xl px-4 pb-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {chat.error && (
          <p role="alert" className="mb-2 text-sm text-red-600 dark:text-red-400">
            {chat.error}
          </p>
        )}
        <Composer busy={chat.busy} canStop={canStop} onSend={chat.send} onStop={() => void chat.cancel()} />
      </div>
    </main>
  );
}

function EmptyState({ detail, models }: { detail: ConversationDetail | null; models: ModelList | null }) {
  if (detail) return null;
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
