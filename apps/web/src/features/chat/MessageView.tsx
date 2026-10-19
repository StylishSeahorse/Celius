import { type ReactNode, memo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { fileUrls } from "../../lib/api";
import { desktop } from "../../lib/desktop";
import type { Message } from "../../lib/types";
import { formatBytes } from "../../lib/uploads";
import type { OutboxItem } from "../../offline/outbox";
import type { LiveRun } from "./useChat";

function partText(message: Message, type: string) {
  return message.parts
    .filter((p) => p.type === type)
    .map((p) => p.text ?? "")
    .join("");
}

// Links in replies never navigate the app itself away; the desktop app opens them in the
// system browser.
function ExternalLink({ node: _node, ...props }: React.ComponentProps<"a"> & { node?: unknown }) {
  return (
    <a
      {...props}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        if (desktop && props.href) {
          event.preventDefault();
          desktop.openExternal(props.href);
        }
      }}
    />
  );
}

const EXTENSIONS: Record<string, string> = {
  python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts", tsx: "tsx",
  jsx: "jsx", json: "json", html: "html", css: "css", bash: "sh", sh: "sh", shell: "sh",
  sql: "sql", yaml: "yaml", yml: "yaml", rust: "rs", go: "go", java: "java", c: "c",
  cpp: "cpp", csharp: "cs", markdown: "md", md: "md", toml: "toml", xml: "xml",
};

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node) {
    return textOf((node as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

/** Code blocks get Copy and Download, so generated scripts and files are one click away. */
function CodeBlock({ node: _node, children, ...props }: React.ComponentProps<"pre"> & { node?: unknown }) {
  const [copied, setCopied] = useState(false);
  const code = textOf(children);
  const language = /language-(\w+)/.exec(
    (children as { props?: { className?: string } } | undefined)?.props?.className ?? "",
  )?.[1];
  const download = () => {
    const url = URL.createObjectURL(new Blob([code], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `snippet.${EXTENSIONS[language ?? ""] ?? "txt"}`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="group/code relative">
      <div className="absolute top-1.5 right-1.5 flex gap-1 opacity-100 md:opacity-0 md:group-hover/code:opacity-100">
        <button
          className="rounded bg-canvas/90 px-1.5 py-0.5 text-xs text-muted hover:text-ink"
          onClick={() => void navigator.clipboard?.writeText(code).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          })}
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button className="rounded bg-canvas/90 px-1.5 py-0.5 text-xs text-muted hover:text-ink" onClick={download}>
          Download
        </button>
      </div>
      <pre {...props}>{children}</pre>
    </div>
  );
}

interface FilePartData {
  file_id: string;
  filename: string;
  relative_path?: string | null;
  kind: string;
  size_bytes: number;
  has_thumbnail?: boolean;
}

export function AttachmentList({ message }: { message: Message }) {
  const files = message.parts
    .filter((p) => p.type === "file" && p.data)
    .map((p) => p.data as unknown as FilePartData);
  if (!files.length) return null;
  return (
    <ul className="flex flex-wrap justify-end gap-2" aria-label="Attached files">
      {files.map((f) =>
        f.kind === "image" ? (
          <li key={f.file_id}>
            <a href={fileUrls.inline(f.file_id)} target="_blank" rel="noopener noreferrer" title={f.filename}>
              <img
                src={fileUrls.thumbnail(f.file_id)}
                alt={f.filename}
                loading="lazy"
                className="h-28 max-w-52 rounded-lg border border-line object-cover"
                onError={(e) => {
                  e.currentTarget.style.display = "none";
                }}
              />
            </a>
          </li>
        ) : (
          <li key={f.file_id}>
            <a
              href={fileUrls.download(f.file_id)}
              download
              className="flex max-w-64 items-center gap-2 rounded-lg border border-line bg-canvas px-3 py-1.5 text-sm hover:border-accent/60"
              title={`Download ${f.relative_path ?? f.filename}`}
            >
              <span aria-hidden="true">📄</span>
              <span className="truncate">{f.relative_path ?? f.filename}</span>
              <span className="shrink-0 text-xs text-muted">{formatBytes(f.size_bytes)}</span>
            </a>
          </li>
        ),
      )}
    </ul>
  );
}

const AssistantMarkdown = memo(function AssistantMarkdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <Markdown remarkPlugins={[remarkGfm]} components={{ a: ExternalLink, pre: CodeBlock }}>
        {text}
      </Markdown>
    </div>
  );
});

export interface MessageActions {
  disabled: boolean;
  onSelect: (messageId: string) => void;
  onRegenerate: (messageId: string) => void;
  onEdit: (messageId: string, content: string, attachmentIds: string[]) => void;
}

const actionButton =
  "rounded-md px-2 py-1 text-xs text-muted hover:bg-panel hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent";

function VersionSwitcher({ message, actions }: { message: Message; actions: MessageActions }) {
  const siblings = message.sibling_ids;
  if (siblings.length < 2) return null;
  const index = siblings.indexOf(message.id);
  return (
    <span className="inline-flex items-center text-xs text-muted" aria-label="Versions">
      <button
        className={actionButton}
        disabled={actions.disabled || index <= 0}
        onClick={() => actions.onSelect(siblings[index - 1])}
        aria-label="Previous version"
      >
        ‹
      </button>
      <span>
        {index + 1} / {siblings.length}
      </span>
      <button
        className={actionButton}
        disabled={actions.disabled || index >= siblings.length - 1}
        onClick={() => actions.onSelect(siblings[index + 1])}
        aria-label="Next version"
      >
        ›
      </button>
    </span>
  );
}

function UserMessage({ message, actions }: { message: Message; actions: MessageActions }) {
  const text = partText(message, "text");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);

  if (editing) {
    return (
      <div className="flex justify-end">
        <div className="w-full max-w-[85%] space-y-2 rounded-2xl border border-line bg-panel p-3">
          <textarea
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Edit message"
            rows={Math.min(10, Math.max(2, draft.split("\n").length))}
            className="block w-full resize-y bg-transparent leading-relaxed outline-none"
          />
          <div className="flex justify-end gap-2">
            <button className="rounded-md px-3 py-1 text-sm hover:bg-canvas" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button
              className="rounded-md bg-accent px-3 py-1 text-sm text-white disabled:opacity-40"
              disabled={draft.trim() === text.trim() || actions.disabled}
              onClick={() => {
                // An edit keeps the original attachments.
                const ids = message.parts
                  .filter((p) => p.type === "file" && p.data)
                  .map((p) => (p.data as unknown as FilePartData).file_id);
                actions.onEdit(message.id, draft.trim(), ids);
                setEditing(false);
              }}
            >
              Save & send
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group flex flex-col items-end gap-1">
      <AttachmentList message={message} />
      {text && (
        <div data-testid="user-message" className="max-w-[85%] whitespace-pre-wrap rounded-2xl bg-panel px-4 py-2.5 leading-relaxed">
          {text}
        </div>
      )}
      <div className="flex items-center gap-1 opacity-100 md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100">
        <VersionSwitcher message={message} actions={actions} />
        <button
          className={actionButton}
          disabled={actions.disabled}
          onClick={() => {
            setDraft(text);
            setEditing(true);
          }}
        >
          Edit
        </button>
      </div>
    </div>
  );
}

export function MessageView({
  message,
  live,
  actions,
}: {
  message: Message;
  live: LiveRun | null;
  actions: MessageActions;
}) {
  const [copied, setCopied] = useState(false);
  if (message.role === "user") return <UserMessage message={message} actions={actions} />;

  const isLive = live !== null && live.messageId === message.id;
  const text = isLive ? live.text : partText(message, "text");
  const reasoning = isLive ? live.reasoning : partText(message, "reasoning");
  const status = isLive ? live.status : message.status;
  const inProgress = ["queued", "running", "reconnecting", "pending", "streaming"].includes(status);
  const waiting = inProgress && !text;

  return (
    <div className="group space-y-2">
      {reasoning && (
        <details className="rounded-lg border border-line px-3 py-2 text-sm text-muted">
          <summary className="cursor-pointer select-none">Reasoning</summary>
          <p className="mt-2 whitespace-pre-wrap">{reasoning}</p>
        </details>
      )}
      {waiting ? (
        <div className="flex items-center gap-2 text-muted" aria-live="polite">
          <span className="inline-flex gap-1">
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted [animation-delay:-0.2s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted [animation-delay:-0.1s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted" />
          </span>
          {status === "queued" || status === "pending"
            ? "Waiting for the model…"
            : status === "reconnecting"
              ? "Reconnecting…"
              : null}
        </div>
      ) : (
        <AssistantMarkdown text={text} />
      )}
      {status === "cancelled" && <p className="text-xs text-muted">Stopped</p>}
      {status === "failed" && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {(isLive && live.error) || "The response could not be generated."}
        </p>
      )}
      {isLive && status === "reconnecting" && text && <p className="text-xs text-muted">Reconnecting…</p>}
      {!inProgress && (
        <div className="flex items-center gap-1 opacity-100 md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100">
          <VersionSwitcher message={message} actions={actions} />
          <button
            className={actionButton}
            onClick={() => {
              void navigator.clipboard?.writeText(text).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              });
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            className={actionButton}
            disabled={actions.disabled}
            onClick={() => actions.onRegenerate(message.id)}
          >
            Regenerate
          </button>
        </div>
      )}
    </div>
  );
}

export function PendingMessage({
  item,
  online,
  onRetry,
  onDiscard,
}: {
  item: OutboxItem;
  online: boolean;
  onRetry: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="flex flex-col items-end gap-1" data-testid="pending-message">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl border border-dashed border-line bg-panel/60 px-4 py-2.5 leading-relaxed">
        {item.content}
        {item.attachments?.length ? (
          <span className="mt-1 block text-xs text-muted">
            📎 {item.attachments.map((a) => a.filename).join(", ")}
          </span>
        ) : null}
      </div>
      <p className="text-xs text-muted">
        {item.status === "failed" ? (
          <span className="text-red-600 dark:text-red-400">
            Not sent: {item.error}{" "}
            <button className="underline" onClick={onRetry}>
              Retry
            </button>{" "}
            <button className="underline" onClick={onDiscard}>
              Discard
            </button>
          </span>
        ) : online ? (
          "Sending…"
        ) : (
          "Queued — will send when you're back online"
        )}
      </p>
    </div>
  );
}
