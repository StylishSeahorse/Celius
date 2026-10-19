import { type FormEvent, useState } from "react";

import type { Project } from "../../lib/types";

interface Props {
  project: Project | null; // null: create
  onSave: (fields: { name: string; instructions: string | null }) => Promise<void>;
  onDelete?: () => Promise<void>;
  onClose: () => void;
}

export function ProjectDialog({ project, onSave, onDelete, onClose }: Props) {
  const [name, setName] = useState(project?.name ?? "");
  const [instructions, setInstructions] = useState(project?.instructions ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await onSave({ name: name.trim(), instructions: instructions.trim() || null });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-dialog-title"
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-lg space-y-4 rounded-2xl bg-canvas p-6 shadow-xl"
      >
        <h2 id="project-dialog-title" className="text-lg font-semibold">
          {project ? "Project settings" : "New project"}
        </h2>
        <label className="block space-y-1">
          <span className="text-sm text-muted">Name</span>
          <input
            autoFocus
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-sm text-muted">Instructions for every chat in this project</span>
          <textarea
            rows={5}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="e.g. Answer as a concise senior engineer. Prefer TypeScript examples."
            className="w-full resize-y rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
        <div className="flex items-center gap-2">
          {project && onDelete && (
            <button
              type="button"
              onClick={() => {
                if (confirm(`Delete project “${project.name}”? Its chats are kept.`)) void onDelete().then(onClose);
              }}
              className="mr-auto text-sm text-red-600 hover:underline dark:text-red-400"
            >
              Delete project
            </button>
          )}
          <button type="button" onClick={onClose} className="ml-auto rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel">
            Cancel
          </button>
          <button
            disabled={busy || !name.trim()}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm text-white hover:bg-accent-strong disabled:opacity-40"
          >
            Save
          </button>
        </div>
      </form>
    </div>
  );
}
