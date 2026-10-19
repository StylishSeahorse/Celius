import { type ClipboardEvent, type DragEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

import { ApiError, isNetworkError } from "../../lib/api";
import { newId } from "../../lib/ids";
import type { StoredFile } from "../../lib/types";
import { formatBytes, relativePathOf, uploadFile, waitUntilProcessed } from "../../lib/uploads";

const MAX_ATTACHMENTS = 20;

export interface Attachment {
  key: string;
  name: string;
  size: number;
  isImage: boolean;
  progress: number;
  state: "uploading" | "processing" | "ready" | "unreadable" | "error";
  file?: StoredFile;
  error?: string;
}

interface Props {
  canStop: boolean;
  online: boolean;
  vision: boolean;
  reasoning: boolean | null;
  reasoningSupported: boolean;
  /** Extra attach sources (the desktop app's approved folders). */
  extraAttach?: { label: string; pick: () => Promise<File[]> };
  onSend: (text: string, attachments: StoredFile[]) => Promise<void>;
  onStop: () => void;
  onReasoning: (value: boolean | null) => void;
  onStorageChanged?: () => void;
}

const REASONING_NEXT: Record<string, boolean | null> = { null: true, true: false, false: null };
const REASONING_LABEL: Record<string, string> = { null: "Auto", true: "On", false: "Off" };

export function Composer(props: Props) {
  const { canStop, online, vision, reasoning, onSend, onStop } = props;
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [text]);

  const update = (key: string, patch: Partial<Attachment>) =>
    setAttachments((list) => list.map((a) => (a.key === key ? { ...a, ...patch } : a)));

  async function add(files: File[]) {
    if (!online) return;
    const room = MAX_ATTACHMENTS - attachments.length;
    for (const file of files.slice(0, Math.max(room, 0))) {
      const key = newId();
      setAttachments((list) => [
        ...list,
        { key, name: relativePathOf(file) ?? file.name, size: file.size, isImage: file.type.startsWith("image/"), progress: 0, state: "uploading" },
      ]);
      try {
        const stored = await uploadFile(file, {
          relativePath: relativePathOf(file),
          onProgress: (progress) => update(key, { progress }),
        });
        update(key, { state: "processing", progress: 1, file: stored });
        const done = await waitUntilProcessed(stored);
        update(key, { state: done.status === "ready" ? "ready" : "unreadable", file: done, error: done.error ?? undefined });
      } catch (err) {
        update(key, {
          state: "error",
          error: isNetworkError(err) ? "You're offline" : err instanceof ApiError ? err.message : "Upload failed",
        });
      } finally {
        props.onStorageChanged?.();
      }
    }
  }

  const busyUploading = attachments.some((a) => a.state === "uploading" || a.state === "processing");
  const sendable = attachments.filter((a) => a.file && (a.state === "ready" || a.state === "unreadable"));
  const canSend = (text.trim().length > 0 || sendable.length > 0) && !busyUploading;

  async function submit() {
    const content = text.trim();
    if (!canSend) return;
    const files = sendable.map((a) => a.file!);
    const previous = attachments;
    setText("");
    setAttachments([]);
    try {
      await onSend(content, files);
    } catch {
      setText(content); // keep the draft so nothing typed is lost
      setAttachments(previous);
    }
    ref.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  }

  function onPaste(event: ClipboardEvent) {
    const files = [...event.clipboardData.files];
    if (files.length) {
      event.preventDefault();
      void add(files);
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    void add([...event.dataTransfer.files]);
  }

  const imageWithoutVision = !vision && attachments.some((a) => a.isImage && a.state !== "error");
  const reasoningKey = String(reasoning);

  return (
    <div
      onDragOver={(e) => {
        if (!online) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={`rounded-2xl border bg-canvas p-2 shadow-sm focus-within:border-accent/60 ${dragging ? "border-accent" : "border-line"}`}
    >
      {attachments.length > 0 && (
        <ul className="flex flex-wrap gap-2 px-1 pt-1 pb-2" aria-label="Attachments">
          {attachments.map((a) => (
            <li
              key={a.key}
              data-testid="attachment-chip"
              data-state={a.state}
              className="flex max-w-56 items-center gap-2 rounded-lg border border-line bg-panel px-2 py-1 text-xs"
            >
              <span className="truncate" title={a.name}>
                {a.name}
              </span>
              <span className="shrink-0 text-muted">
                {a.state === "uploading"
                  ? `${Math.round(a.progress * 100)}%`
                  : a.state === "processing"
                    ? "Reading…"
                    : a.state === "error"
                      ? "Failed"
                      : a.state === "unreadable"
                        ? "Can't read"
                        : formatBytes(a.size)}
              </span>
              <button
                onClick={() => setAttachments((list) => list.filter((x) => x.key !== a.key))}
                aria-label={`Remove ${a.name}`}
                className="shrink-0 text-muted hover:text-ink"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {attachments.some((a) => a.state === "error") && (
        <p role="alert" className="px-2 pb-1 text-xs text-red-600 dark:text-red-400">
          {attachments.find((a) => a.state === "error")?.error}
        </p>
      )}
      {imageWithoutVision && (
        <p className="px-2 pb-1 text-xs text-muted">The current model can't see images; it will only get the file name.</p>
      )}
      <textarea
        ref={ref}
        // Ready to type on desktop; not on touch screens, where it would pop the keyboard.
        autoFocus={window.matchMedia?.("(pointer: fine)").matches}
        rows={1}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        placeholder="Message Celius…"
        aria-label="Message"
        className="block max-h-60 w-full resize-none bg-transparent px-2 py-1.5 leading-relaxed outline-none"
      />
      <div className="flex items-center gap-1">
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          aria-label="Choose files"
          onChange={(e) => {
            void add([...(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
        <input
          ref={folderInput}
          type="file"
          hidden
          aria-label="Choose a folder"
          // @ts-expect-error -- non-standard but supported by every current browser
          webkitdirectory=""
          onChange={(e) => {
            void add([...(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
        <button
          onClick={() => fileInput.current?.click()}
          disabled={!online}
          title={online ? "Attach files" : "Attaching needs a connection"}
          className="rounded-lg px-2 py-1 text-sm text-muted hover:bg-panel hover:text-ink disabled:opacity-40"
        >
          Attach
        </button>
        <button
          onClick={() => folderInput.current?.click()}
          disabled={!online}
          title="Attach a folder"
          className="hidden rounded-lg px-2 py-1 text-sm text-muted hover:bg-panel hover:text-ink disabled:opacity-40 sm:inline"
        >
          Folder
        </button>
        {props.extraAttach && (
          <button
            onClick={() => void props.extraAttach!.pick().then(add)}
            disabled={!online}
            className="rounded-lg px-2 py-1 text-sm text-muted hover:bg-panel hover:text-ink disabled:opacity-40"
          >
            {props.extraAttach.label}
          </button>
        )}
        <button
          onClick={() => props.onReasoning(REASONING_NEXT[reasoningKey])}
          aria-label={`Thinking: ${REASONING_LABEL[reasoningKey]}`}
          title={
            props.reasoningSupported
              ? "Model thinking before it answers. Auto uses the model's default."
              : "Thinking control: this model's chat template may ignore it."
          }
          className={`rounded-lg px-2 py-1 text-sm hover:bg-panel ${reasoning === true ? "text-accent" : "text-muted hover:text-ink"}`}
        >
          Thinking: {REASONING_LABEL[reasoningKey]}
        </button>
        <span className="flex-1" />
        {canStop ? (
          <button onClick={onStop} className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel">
            Stop
          </button>
        ) : (
          <button
            onClick={() => void submit()}
            disabled={!canSend}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-40"
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}
