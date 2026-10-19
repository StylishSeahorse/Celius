import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, fileUrls } from "../../lib/api";
import type { Project, StoredFile } from "../../lib/types";
import { formatBytes, relativePathOf, uploadFile } from "../../lib/uploads";
import { runExport } from "./exporting";

interface Props {
  project: Project | null; // null: create
  online: boolean;
  onSave: (fields: { name: string; instructions: string | null }) => Promise<void>;
  onDelete?: () => Promise<void>;
  onClose: () => void;
  onStorageChanged: () => void;
}

export function ProjectDialog({ project, online, onSave, onDelete, onClose, onStorageChanged }: Props) {
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
        className="max-h-[90vh] w-full max-w-lg space-y-4 overflow-y-auto rounded-2xl bg-canvas p-6 shadow-xl"
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
            rows={4}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="e.g. Answer as a concise senior engineer. Prefer TypeScript examples."
            className="w-full resize-y rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          />
        </label>
        {project && <ProjectFiles project={project} online={online} onStorageChanged={onStorageChanged} />}
        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
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
          {project && online && <ExportProject projectId={project.id} />}
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

function ExportProject({ projectId }: { projectId: string }) {
  const [status, setStatus] = useState<string | null>(null);
  return (
    <button
      type="button"
      onClick={() => {
        setStatus("Preparing…");
        runExport({ scope: "project", id: projectId, format: "zip" })
          .then(() => setStatus(null))
          .catch((err: unknown) => setStatus(err instanceof Error ? err.message : "Export failed"));
      }}
      className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel"
    >
      {status ?? "Export ZIP"}
    </button>
  );
}

/** Knowledge files: their text is given to the model in every chat of the project. */
function ProjectFiles({ project, online, onStorageChanged }: { project: Project; online: boolean; onStorageChanged: () => void }) {
  const [files, setFiles] = useState<StoredFile[]>([]);
  const [uploading, setUploading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    api.listFiles(project.id).then(setFiles).catch(() => undefined);
  }, [project.id]);

  useEffect(() => {
    load();
    window.addEventListener("celius:files-changed", load);
    return () => window.removeEventListener("celius:files-changed", load);
  }, [load]);

  async function add(list: File[]) {
    setError(null);
    for (const file of list) {
      setUploading(file.name);
      try {
        await uploadFile(file, { projectId: project.id, relativePath: relativePathOf(file) });
      } catch (err) {
        setError(`${file.name}: ${err instanceof ApiError ? err.message : "upload failed"}`);
      }
    }
    setUploading(null);
    load();
    onStorageChanged();
    // Text extraction runs in the background; refresh once it has had time to finish.
    setTimeout(load, 1500);
  }

  return (
    <section className="space-y-2" aria-label="Project files">
      <div className="flex items-center justify-between">
        <h3 className="text-sm text-muted">Files available to every chat</h3>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          aria-label="Add project files"
          onChange={(e) => {
            void add([...(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
        <button
          type="button"
          disabled={!online || uploading !== null}
          onClick={() => input.current?.click()}
          className="rounded-md border border-line px-2 py-1 text-xs hover:bg-panel disabled:opacity-40"
        >
          {uploading ? `Uploading ${uploading}…` : "Add files"}
        </button>
      </div>
      {files.length === 0 ? (
        <p className="text-xs text-muted">No files yet. Add documents, notes or code the model should know about.</p>
      ) : (
        <ul className="max-h-48 divide-y divide-line overflow-y-auto rounded-lg border border-line text-sm">
          {files.map((f) => (
            <li key={f.id} className="flex items-center gap-2 px-3 py-1.5">
              <a href={fileUrls.download(f.id)} download className="min-w-0 flex-1 truncate hover:underline">
                {f.relative_path ?? f.filename}
              </a>
              <span className="shrink-0 text-xs text-muted">
                {f.status === "processing" ? "Reading…" : f.status === "failed" ? "Can't read" : formatBytes(f.size_bytes)}
              </span>
              <button
                type="button"
                aria-label={`Remove ${f.filename}`}
                disabled={!online}
                onClick={() => void api.deleteFile(f.id).then(() => { load(); onStorageChanged(); })}
                className="shrink-0 text-muted hover:text-ink disabled:opacity-40"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </section>
  );
}
