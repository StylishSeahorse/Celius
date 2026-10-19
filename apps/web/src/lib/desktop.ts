/**
 * Features offered by the Celius desktop app (apps/desktop). In a normal browser `desktop`
 * is null. In the desktop app, the native side grants this server's pages a few commands:
 * approved-folder access (read-only, path-checked natively) and opening links externally.
 */

interface TauriInternals {
  invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T>;
}

export interface DesktopRoot {
  id: string;
  name: string;
  path: string;
}

export interface DesktopEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
}

const internals = (window as Window & { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;

export const desktop = internals
  ? {
      listRoots: () => internals.invoke<DesktopRoot[]>("list_roots"),
      addRoot: () => internals.invoke<DesktopRoot | null>("add_root"),
      removeRoot: (id: string) => internals.invoke<void>("remove_root", { id }),
      listDir: (rootId: string, path: string) => internals.invoke<DesktopEntry[]>("list_dir", { rootId, path }),
      async readFile(root: DesktopRoot, entry: DesktopEntry): Promise<File> {
        const bytes = await internals.invoke<ArrayBuffer>("read_file", { rootId: root.id, path: entry.path });
        const file = new File([bytes], entry.name);
        // Keep the folder structure, like a folder picked in the browser.
        Object.defineProperty(file, "celiusRelativePath", { value: `${root.name}/${entry.path}` });
        return file;
      },
      openExternal: (url: string) => void internals.invoke("open_external", { url }).catch(() => window.open(url, "_blank")),
    }
  : null;
