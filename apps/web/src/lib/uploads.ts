import { ApiError, api } from "./api";
import type { StoredFile } from "./types";

export interface UploadOptions {
  relativePath?: string | null;
  projectId?: string | null;
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/** PUT the bytes with XMLHttpRequest: unlike fetch, it reports upload progress. */
function putContent(url: string, file: Blob, onProgress?: (f: number) => void, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let error: { code?: string; message?: string } = {};
      try {
        error = JSON.parse(xhr.responseText).error ?? {};
      } catch {
        /* not JSON */
      }
      reject(new ApiError(xhr.status, error.code ?? `http_${xhr.status}`, error.message ?? "Upload failed"));
    };
    xhr.onerror = () => reject(new TypeError("Network error during upload"));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort());
    xhr.send(file);
  });
}

/** Upload a file: reserve → send bytes → verify. Resolves once the server has stored it. */
export async function uploadFile(file: File, options: UploadOptions = {}): Promise<StoredFile> {
  const session = await api.createUpload({
    filename: file.name,
    size: file.size,
    relative_path: options.relativePath ?? null,
    project_id: options.projectId ?? null,
  });
  await putContent(session.upload_url, file, options.onProgress, options.signal);
  return api.completeUpload(session.id);
}

/** Wait until text extraction / thumbnailing has finished. */
export async function waitUntilProcessed(file: StoredFile, signal?: AbortSignal): Promise<StoredFile> {
  let current = file;
  for (let delay = 300; current.status === "processing"; delay = Math.min(delay * 1.5, 2000)) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    current = await api.getFile(file.id);
  }
  return current;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** The path of a file chosen through a folder picker or dropped as part of a folder. */
export function relativePathOf(file: File): string | null {
  const path =
    (file as File & { celiusRelativePath?: string }).celiusRelativePath ??
    (file as File & { webkitRelativePath?: string }).webkitRelativePath;
  return path && path.includes("/") ? path : null;
}
