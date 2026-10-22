import { type ReactNode, memo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { fileUrls } from "../../lib/api";
import { desktop } from "../../lib/desktop";
import type { Message } from "../../lib/types";
import { formatBytes } from "../../lib/uploads";
import type { OutboxItem } from "../../offline/outbox";
import { ToolCallCard, toolLabel } from "../tools/ToolCallCard";
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

export function AttachmentList({ message, align = "end" }: { message: Message; align?: "start" | "end" }) {
  const files = message.parts
    .filter((p) => p.type === "file" && p.data)
    .map((p) => p.data as unknown as FilePartData);
  if (!files.length) return null;
  return (
    <ul
      className={`flex flex-wrap gap-2 ${align === "end" ? "justify-end" : "justify-start"}`}
      aria-label="Attached files"
    >
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
        ) : f.kind === "audio" || f.kind === "video" ? (
          <li key={f.file_id} className="w-full max-w-md">
            <MediaAttachment file={f} />
          </li>
        ) : f.has_thumbnail ? (
          <li key={f.file_id}>
            <a
              href={f.kind === "pdf" ? fileUrls.inline(f.file_id) : fileUrls.download(f.file_id)}
              {...(f.kind === "pdf" ? { target: "_blank", rel: "noopener noreferrer" } : { download: true })}
              className="flex w-40 flex-col overflow-hidden rounded-lg border border-line bg-canvas text-sm hover:border-accent/60"
              title={f.kind === "pdf" ? `Open ${f.filename}` : `Download ${f.filename}`}
              data-testid="file-preview"
            >
              <img
                src={fileUrls.thumbnail(f.file_id)}
                alt={`Preview of ${f.filename}`}
                loading="lazy"
                className="h-28 w-full border-b border-line bg-white object-cover object-top"
                onError={(e) => {
                  e.currentTarget.style.display = "none";
                }}
              />
              <span className="flex items-center gap-1 px-2 py-1">
                <span className="truncate">{f.relative_path ?? f.filename}</span>
                <span className="ml-auto shrink-0 text-xs text-muted">{formatBytes(f.size_bytes)}</span>
              </span>
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

/** Audio and video play in place; seeking works (the server answers range requests). */
function MediaAttachment({ file }: { file: FilePartData }) {
  const [unplayable, setUnplayable] = useState(false);
  const name = file.relative_path ?? file.filename;
  const common = {
    src: fileUrls.inline(file.file_id),
    controls: true,
    preload: "metadata" as const,
    onError: () => setUnplayable(true),
    "aria-label": name,
  };
  return (
    <figure className="space-y-1 rounded-lg border border-line bg-canvas p-2" data-testid="media-attachment">
      {unplayable ? (
        <p className="px-1 text-sm text-muted">This browser can't play this {file.kind} file. Download it to play it.</p>
      ) : file.kind === "video" ? (
        <video
          {...common}
          playsInline
          poster={file.has_thumbnail ? fileUrls.thumbnail(file.file_id) : undefined}
          className="max-h-80 w-full rounded-md bg-black"
        />
      ) : (
        <audio {...common} className="w-full" />
      )}
      <figcaption className="flex items-center gap-2 px-1 text-xs text-muted">
        <span className="truncate">{name}</span>
        <span className="shrink-0">{formatBytes(file.size_bytes)}</span>
        <a href={fileUrls.download(file.file_id)} download className="ml-auto shrink-0 hover:text-ink hover:underline">
          Download
        </a>
      </figcaption>
    </figure>
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

interface MemoryUse {
  memories: { id: string; content: string }[];
  recalled: { conversation_id: string; title: string; excerpt: string }[];
}

/** Which memories and earlier chats this reply was given. */
function MemoryUsed({ data }: { data: MemoryUse }) {
  const parts = [];
  if (data.memories.length) parts.push(`${data.memories.length} ${data.memories.length === 1 ? "memory" : "memories"}`);
  if (data.recalled.length) parts.push(`${data.recalled.length} earlier ${data.recalled.length === 1 ? "chat" : "chats"}`);
  if (!parts.length) return null;
  return (
    <details className="text-xs text-muted" data-testid="memory-used">
      <summary className="cursor-pointer select-none">🧠 Used {parts.join(" and ")}</summary>
      <ul className="mt-1 space-y-0.5 pl-4">
        {data.memories.map((m) => (
          <li key={m.id}>• {m.content}</li>
        ))}
        {data.recalled.map((r, i) => (
          <li key={`${r.conversation_id}-${i}`}>
            • from <a className="underline" href={`#/c/${r.conversation_id}`}>{r.title}</a>
          </li>
        ))}
      </ul>
    </details>
  );
}

function Reasoning({ text }: { text: string }) {
  return <p className="whitespace-pre-wrap border-l-2 border-line pl-3 text-muted">{text}</p>;
}

type Part = Message["parts"][number];
type Call = NonNullable<Message["tool_calls"]>[number];

/**
 * A reply is shown as its answer (the text after the last tool call), with everything
 * that led to it (reasoning, tool calls and what was said between them) folded into one
 * panel. Tool calls waiting for approval stay outside it, so they can't be missed.
 */
function splitReply(parts: Part[]) {
  let lastTool = -1;
  parts.forEach((p, i) => {
    if (p.type === "tool_call") lastTool = i;
  });
  const work: { part: Part; index: number }[] = [];
  const answer: { part: Part; index: number }[] = [];
  parts.forEach((part, index) => {
    if (part.type === "reasoning" || part.type === "tool_call") work.push({ part, index });
    else if (part.type === "text") (index < lastTool ? work : answer).push({ part, index });
    else answer.push({ part, index });
  });
  return { work, answer };
}

const capitalise = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

function WorkPanel({
  items,
  callOf,
  liveReasoning,
  inProgress,
  onToolDecided,
}: {
  items: { part: Part; index: number }[];
  callOf: (part: Part) => Call | undefined;
  liveReasoning: string;
  inProgress: boolean;
  onToolDecided?: () => void;
}) {
  const calls = items.map((i) => callOf(i.part)).filter((c): c is Call => !!c);
  const thought = liveReasoning !== "" || items.some((i) => i.part.type === "reasoning" && i.part.text);
  if (!calls.length && !thought) return null;
  const running = calls.find((c) => c.status === "running" || c.status === "approved");
  const failed = calls.filter((c) => ["failed", "refused", "denied", "expired", "interrupted"].includes(c.status)).length;
  const summary = [
    thought && !inProgress ? "Reasoning" : null,
    calls.length ? `used ${calls.length} ${calls.length === 1 ? "tool" : "tools"}` : null,
    failed ? `${failed} didn't work` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const activity = running ? `Running ${toolLabel(running.tool_name)}…` : liveReasoning ? "Thinking…" : "Working…";

  return (
    <details className="group/work rounded-lg border border-line text-sm" data-testid="work-panel">
      <summary className="flex cursor-pointer select-none items-center gap-2 px-3 py-2 text-muted">
        <span aria-hidden="true" className="transition-transform group-open/work:rotate-90">
          ›
        </span>
        {inProgress && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden="true" />}
        <span>{capitalise(inProgress ? [activity, summary].filter(Boolean).join(" · ") : summary)}</span>
      </summary>
      <div className="space-y-2 border-t border-line px-3 py-2">
        {items.map(({ part, index }) => {
          if (part.type === "reasoning") return part.text ? <Reasoning key={index} text={part.text} /> : null;
          if (part.type === "text")
            return part.text ? (
              <div key={index} className="text-muted">
                <AssistantMarkdown text={part.text} />
              </div>
            ) : null;
          const call = callOf(part);
          return call && call.status !== "pending_approval" ? (
            <ToolCallCard key={index} call={call} onDecided={onToolDecided} />
          ) : null;
        })}
        {liveReasoning && <Reasoning text={liveReasoning} />}
      </div>
    </details>
  );
}

export function MessageView({
  message,
  live,
  actions,
  onToolDecided,
}: {
  message: Message;
  live: LiveRun | null;
  actions: MessageActions;
  onToolDecided?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  if (message.role === "user") return <UserMessage message={message} actions={actions} />;

  const isLive = live !== null && live.messageId === message.id;
  const text = partText(message, "text") + (isLive ? live.text : "");
  const status = isLive ? live.status : message.status;
  const inProgress = ["queued", "running", "reconnecting", "pending", "streaming"].includes(status);
  const calls = new Map((message.tool_calls ?? []).map((c) => [c.id, c]));
  const callOf = (part: Part) => {
    const id = (part.data as { tool_call_id?: string } | null)?.tool_call_id ?? "";
    return (isLive && live.toolCalls[id]) || calls.get(id);
  };
  const { work, answer } = splitReply(message.parts);
  const hasSteps = message.parts.some((p) => p.type !== "text" || p.text);
  const waiting = inProgress && !(isLive && (live.text || live.reasoning));

  return (
    <div className="group space-y-2">
      <WorkPanel
        items={work}
        callOf={callOf}
        liveReasoning={isLive ? live.reasoning : ""}
        inProgress={inProgress}
        onToolDecided={onToolDecided}
      />
      {work.map(({ part, index }) => {
        const call = part.type === "tool_call" ? callOf(part) : undefined;
        return call?.status === "pending_approval" ? (
          <ToolCallCard key={index} call={call} onDecided={onToolDecided} />
        ) : null;
      })}
      {answer.map(({ part, index }) => {
        if (part.type === "text" && part.text) return <AssistantMarkdown key={index} text={part.text} />;
        if (part.type === "memory" && part.data) return <MemoryUsed key={index} data={part.data as unknown as MemoryUse} />;
        return null;
      })}
      {!inProgress && <AttachmentList message={message} align="start" />}
      {isLive && live.text && <AssistantMarkdown text={live.text} />}
      {status === "awaiting_approval" ? (
        <p className="text-sm text-amber-700 dark:text-amber-400" aria-live="polite">
          Waiting for your approval above.
        </p>
      ) : waiting ? (
        <div className="flex items-center gap-2 text-muted" aria-live="polite">
          <span className="inline-flex gap-1">
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted [animation-delay:-0.2s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted [animation-delay:-0.1s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted" />
          </span>
          {(status === "queued" || status === "pending") && !hasSteps
            ? "Waiting for the model…"
            : status === "reconnecting"
              ? "Reconnecting…"
              : null}
        </div>
      ) : null}
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
