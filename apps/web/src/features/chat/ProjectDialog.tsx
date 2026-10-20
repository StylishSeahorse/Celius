import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ApiError, type ProjectMember, api, fileUrls } from "../../lib/api";
import type { Project, StoredFile } from "../../lib/types";
import { formatBytes, relativePathOf, uploadFile } from "../../lib/uploads";
import { runExport } from "./exporting";

interface Props {
  project: Project | null; // null: create
  me: string; // your username (to leave a project shared with you)
  online: boolean;
  onSave: (fields: { name: string; instructions: string | null; memory_enabled: boolean }) => Promise<void>;
  onDelete?: () => Promise<void>;
  onClose: () => void;
  onStorageChanged: () => void;
}

export function ProjectDialog({ project, me, online, onSave, onDelete, onClose, onStorageChanged }: Props) {
  const role = project?.role ?? "owner";
  const isOwner = role === "owner";
  const readOnly = role === "read";
  const [name, setName] = useState(project?.name ?? "");
  const [instructions, setInstructions] = useState(project?.instructions ?? "");
  const [memory, setMemory] = useState(project?.memory_enabled ?? true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await onSave({ name: name.trim(), instructions: instructions.trim() || null, memory_enabled: memory });
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
        {project && !isOwner && (
          <p className="rounded-lg bg-panel px-3 py-2 text-sm" data-testid="shared-with-me">
            Shared with you by <strong>{project.owner}</strong>
            {readOnly ? " (you can use it, not change it)" : " (you can change its instructions and files)"}.
            Your chats in it stay private.
          </p>
        )}
        <label className="block space-y-1">
          <span className="text-sm text-muted">Name</span>
          <input
            autoFocus
            required
            maxLength={200}
            disabled={readOnly}
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-sm text-muted">Instructions for every chat in this project</span>
          <textarea
            rows={4}
            disabled={readOnly}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="e.g. Answer as a concise senior engineer. Prefer TypeScript examples."
            className="w-full resize-y rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          />
        </label>
        <label className={`flex items-start gap-2 text-sm ${isOwner ? "" : "hidden"}`}>
          <input type="checkbox" className="mt-1" checked={memory} onChange={(e) => setMemory(e.target.checked)} />
          <span>
            Memory in this project
            <span className="block text-xs text-muted">
              Off: its chats aren't remembered or recalled and get no memories. Turning it off
              removes its chats from recall now.
            </span>
          </span>
        </label>
        {project && (
          <ProjectFiles project={project} online={online} readOnly={readOnly} onStorageChanged={onStorageChanged} />
        )}
        {project && isOwner && <Sharing projectId={project.id} />}
        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {project && !isOwner && (
            <button
              type="button"
              onClick={() => {
                if (confirm(`Leave “${project.name}”? Your chats in it are kept.`))
                  void api.unshareProject(project.id, me).then(() => onDelete?.()).then(onClose);
              }}
              className="mr-auto text-sm text-red-600 hover:underline dark:text-red-400"
            >
              Leave project
            </button>
          )}
          {project && isOwner && onDelete && (
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
          {project && isOwner && online && <ExportProject projectId={project.id} />}
          <button type="button" onClick={onClose} className="ml-auto rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel">
            Cancel
          </button>
          {!readOnly && (
            <button
              disabled={busy || !name.trim()}
              className="rounded-lg bg-accent px-3 py-1.5 text-sm text-white hover:bg-accent-strong disabled:opacity-40"
            >
              Save
            </button>
          )}
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
function ProjectFiles({
  project,
  online,
  readOnly,
  onStorageChanged,
}: {
  project: Project;
  online: boolean;
  readOnly: boolean;
  onStorageChanged: () => void;
}) {
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
          disabled={!online || uploading !== null || readOnly}
          hidden={readOnly}
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
                hidden={readOnly}
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

/** The owner shares the project with other people on this server (A-04). */
function Sharing({ projectId }: { projectId: string }) {
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<"read" | "write">("read");
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api.projectMembers(projectId).then(setMembers).catch(() => setMembers([]));
  }, [projectId]);
  useEffect(load, [load]);

  async function share() {
    setError(null);
    try {
      await api.shareProject(projectId, username.trim().toLowerCase(), role);
      setUsername("");
      load();
    } catch (err) {
      setError(err instanceof ApiError && err.status === 404 ? "Nobody here has that username" : err instanceof Error ? err.message : "Couldn't share");
    }
  }

  return (
    <section className="space-y-2" aria-label="Sharing">
      <h3 className="text-sm text-muted">Shared with</h3>
      {members.length === 0 ? (
        <p className="text-xs text-muted">Only you. People you share it with use its instructions and files; their chats stay private.</p>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line text-sm">
          {members.map((m) => (
            <li key={m.username} className="flex items-center gap-2 px-3 py-1.5" data-testid="member">
              <span className="flex-1 truncate">{m.display_name || m.username}</span>
              <select
                aria-label={`Access for ${m.username}`}
                value={m.role}
                onChange={(e) => void api.shareProject(projectId, m.username, e.target.value as "read" | "write").then(load)}
                className="rounded border border-line bg-canvas px-1 text-xs"
              >
                <option value="read">can use</option>
                <option value="write">can edit</option>
              </select>
              <button
                type="button"
                onClick={() => void api.unshareProject(projectId, m.username).then(load)}
                className="text-xs text-red-600 hover:underline dark:text-red-400"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <input
          aria-label="Username to share with"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          className="min-w-0 flex-1 rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent"
        />
        <select
          aria-label="Access"
          value={role}
          onChange={(e) => setRole(e.target.value as "read" | "write")}
          className="rounded-md border border-line bg-canvas px-1 text-sm"
        >
          <option value="read">can use</option>
          <option value="write">can edit</option>
        </select>
        <button
          type="button"
          disabled={!username.trim()}
          onClick={() => void share()}
          className="rounded-md border border-line px-2 py-1 text-xs hover:bg-panel disabled:opacity-40"
        >
          Share
        </button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </section>
  );
}
