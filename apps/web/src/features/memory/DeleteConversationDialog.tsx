import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import type { ConversationMemory, ConversationSummary } from "../../lib/types";

/** Deleting a chat says what it takes with it: its recall excerpts always, the memories
 * learned from it only if asked. */
export function DeleteConversationDialog({
  conversation,
  onCancel,
  onDelete,
}: {
  conversation: ConversationSummary;
  onCancel: () => void;
  onDelete: (forgetMemories: boolean) => void;
}) {
  const [info, setInfo] = useState<ConversationMemory | null>(null);
  const [forget, setForget] = useState(false);
  useEffect(() => {
    api.conversationMemory(conversation.id).then(setInfo).catch(() => undefined);
  }, [conversation.id]);
  const title = conversation.title || "Untitled";

  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4" onClick={onCancel}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-title"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md space-y-3 rounded-2xl bg-canvas p-6 shadow-xl"
      >
        <h2 id="delete-title" className="text-lg font-semibold">
          Delete “{title}”?
        </h2>
        <p className="text-sm text-muted">
          The chat disappears from all your devices
          {info && info.excerpts > 0 ? ", and it is removed from recall right away" : ""}.
        </p>
        {info && info.memories_learned > 0 && (
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={forget} onChange={(e) => setForget(e.target.checked)} />
            <span>
              Also forget the {info.memories_learned} {info.memories_learned === 1 ? "memory" : "memories"} learned
              from this chat
              <span className="block text-xs text-muted">Otherwise they stay; you can review them under Memory.</span>
            </span>
          </label>
        )}
        <p className="text-xs text-muted">
          Replies already written in other chats, exports and older backups are not changed.
        </p>
        <div className="flex justify-end gap-2">
          <button onClick={onCancel} className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel">
            Cancel
          </button>
          <button
            onClick={() => onDelete(forget)}
            className="rounded-lg bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-700"
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}
