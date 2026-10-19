import { useCallback, useEffect, useState } from "react";

import { type DesktopEntry, type DesktopRoot, desktop } from "../../lib/desktop";
import { formatBytes } from "../../lib/uploads";

/** Pick files from folders approved in the desktop app. Read-only; paths are checked natively. */
export function FolderBrowser({ onPick, onClose }: { onPick: (files: File[]) => void; onClose: () => void }) {
  const [roots, setRoots] = useState<DesktopRoot[]>([]);
  const [root, setRoot] = useState<DesktopRoot | null>(null);
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<DesktopEntry[]>([]);
  const [selected, setSelected] = useState<Map<string, DesktopEntry>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadRoots = useCallback(async () => {
    const list = await desktop!.listRoots();
    setRoots(list);
    setRoot((current) => current ?? list[0] ?? null);
  }, []);

  useEffect(() => {
    loadRoots().catch((e) => setError(String(e)));
  }, [loadRoots]);

  useEffect(() => {
    if (!root) return setEntries([]);
    desktop!
      .listDir(root.id, path)
      .then((list) => {
        setEntries(list);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, [root, path]);

  async function approve() {
    const added = await desktop!.addRoot().catch((e) => {
      setError(String(e));
      return null;
    });
    if (!added) return;
    await loadRoots();
    setRoot(added);
    setPath("");
  }

  async function attach() {
    if (!root) return;
    setBusy(true);
    try {
      onPick(await Promise.all([...selected.values()].map((entry) => desktop!.readFile(root, entry))));
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const crumbs = path ? path.split("/") : [];
  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="folders-title"
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[85vh] w-full max-w-2xl flex-col gap-3 rounded-2xl bg-canvas p-5 shadow-xl"
      >
        <div className="flex items-center gap-2">
          <h2 id="folders-title" className="flex-1 text-lg font-semibold">
            Attach from your PC
          </h2>
          <button onClick={() => void approve()} className="rounded-md border border-line px-2 py-1 text-sm hover:bg-panel">
            Approve a folder…
          </button>
        </div>
        <p className="text-xs text-muted">
          Celius can only read folders you approve here, and only the files you choose are uploaded.
        </p>
        {roots.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted">No folders approved yet.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-1 text-sm">
              <select
                aria-label="Approved folder"
                value={root?.id ?? ""}
                onChange={(e) => {
                  setRoot(roots.find((r) => r.id === e.target.value) ?? null);
                  setPath("");
                  setSelected(new Map());
                }}
                className="rounded-md border border-line bg-canvas px-2 py-1"
              >
                {roots.map((r) => (
                  <option key={r.id} value={r.id} title={r.path}>
                    {r.name}
                  </option>
                ))}
              </select>
              <button className="text-accent hover:underline" onClick={() => setPath("")}>
                /
              </button>
              {crumbs.map((part, i) => (
                <span key={i}>
                  <button className="hover:underline" onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}>
                    {part}
                  </button>
                  {i < crumbs.length - 1 && " / "}
                </span>
              ))}
              {root && (
                <button
                  className="ml-auto text-xs text-muted hover:text-ink"
                  onClick={() =>
                    void desktop!.removeRoot(root.id).then(() => {
                      setRoot(null);
                      void loadRoots();
                    })
                  }
                >
                  Stop sharing this folder
                </button>
              )}
            </div>
            <ul className="min-h-40 flex-1 divide-y divide-line overflow-y-auto rounded-lg border border-line text-sm" aria-label="Folder contents">
              {entries.map((entry) => (
                <li key={entry.path} className="flex items-center gap-2 px-3 py-1.5">
                  {entry.is_dir ? (
                    <button className="flex-1 truncate text-left hover:underline" onClick={() => setPath(entry.path)}>
                      📁 {entry.name}
                    </button>
                  ) : (
                    <label className="flex flex-1 items-center gap-2 truncate">
                      <input
                        type="checkbox"
                        checked={selected.has(entry.path)}
                        onChange={(e) =>
                          setSelected((current) => {
                            const next = new Map(current);
                            if (e.target.checked) next.set(entry.path, entry);
                            else next.delete(entry.path);
                            return next;
                          })
                        }
                      />
                      <span className="truncate">{entry.name}</span>
                      <span className="ml-auto shrink-0 text-xs text-muted">{formatBytes(entry.size)}</span>
                    </label>
                  )}
                </li>
              ))}
              {entries.length === 0 && <li className="px-3 py-4 text-center text-muted">This folder is empty.</li>}
            </ul>
          </>
        )}
        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel">
            Cancel
          </button>
          <button
            disabled={selected.size === 0 || busy}
            onClick={() => void attach()}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm text-white hover:bg-accent-strong disabled:opacity-40"
          >
            Attach {selected.size || ""} file{selected.size === 1 ? "" : "s"}
          </button>
        </div>
      </div>
    </div>
  );
}
