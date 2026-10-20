import { ApiError, api, isNetworkError } from "./api";
import type { StoredFile } from "./types";

export interface UploadOptions {
  relativePath?: string | null;
  projectId?: string | null;
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/** Files larger than this go up in chunks of this size, so a dropped connection costs at
 * most one chunk (U-11). */
export const CHUNK_BYTES = 4 * 1024 * 1024;
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000];

/** PUT bytes with XMLHttpRequest: unlike fetch, it reports upload progress. Resolves with
 * the number of bytes the server now has. */
function putContent(
  url: string,
  body: Blob,
  onProgress?: (loaded: number) => void,
  signal?: AbortSignal,
  headers: Record<string, string> = {},
) {
  return new Promise<number>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.withCredentials = true;
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (e) => onProgress?.(e.loaded);
    xhr.onload = () => {
      let parsed: { received_bytes?: number; error?: Record<string, unknown> } = {};
      try {
        parsed = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(parsed.received_bytes ?? body.size);
      const { code, message, ...details } = parsed.error ?? {};
      reject(
        new ApiError(
          xhr.status,
          typeof code === "string" ? code : `http_${xhr.status}`,
          typeof message === "string" ? message : "Upload failed",
          details,
        ),
      );
    };
    xhr.onerror = () => reject(new TypeError("Network error during upload"));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort());
    xhr.send(body);
  });
}

function sleep(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(new DOMException("Upload cancelled", "AbortError"));
    });
  });
}

/** Wait for the connection to come back (a phone in a tunnel), then a little longer. */
async function waitForConnection(delay: number, signal?: AbortSignal) {
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    await new Promise<void>((resolve) => window.addEventListener("online", () => resolve(), { once: true }));
  }
  await sleep(delay, signal);
}

/** Send a large file chunk by chunk. After a network failure it asks the server how much
 * arrived and continues from there. */
async function putChunks(
  sessionId: string,
  url: string,
  file: Blob,
  onProgress?: (fraction: number) => void,
  signal?: AbortSignal,
) {
  const total = file.size;
  let offset = 0;
  let failures = 0;
  while (offset < total) {
    const end = Math.min(offset + CHUNK_BYTES, total);
    const start = offset;
    try {
      offset = await putContent(
        url,
        file.slice(start, end),
        (loaded) => onProgress?.((start + loaded) / total),
        signal,
        { "Content-Range": `bytes ${start}-${end - 1}/${total}` },
      );
      failures = 0;
    } catch (error) {
      if (error instanceof ApiError && error.code === "upload_offset_mismatch") {
        offset = Number(error.details.received_bytes ?? 0);
        continue;
      }
      if (!isNetworkError(error) || failures >= RETRY_DELAYS_MS.length) throw error;
      await waitForConnection(RETRY_DELAYS_MS[failures++], signal);
      // Part of the chunk may have arrived: continue from what the server has.
      offset = (await api.uploadProgress(sessionId)).received_bytes;
    }
    onProgress?.(offset / total);
  }
}

/** Upload a file: reserve → send bytes → verify. Resolves once the server has stored it. */
export async function uploadFile(file: File, options: UploadOptions = {}): Promise<StoredFile> {
  const session = await api.createUpload({
    filename: file.name,
    size: file.size,
    relative_path: options.relativePath ?? null,
    project_id: options.projectId ?? null,
  });
  if (file.size > CHUNK_BYTES) {
    await putChunks(session.id, session.upload_url, file, options.onProgress, options.signal);
  } else {
    await putContent(
      session.upload_url,
      file,
      (loaded) => options.onProgress?.(file.size ? loaded / file.size : 1),
      options.signal,
    );
  }
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
