import { memo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Message } from "../../lib/types";
import type { LiveRun } from "./useChat";

function partText(message: Message, type: string) {
  return message.parts
    .filter((p) => p.type === type)
    .map((p) => p.text ?? "")
    .join("");
}

const AssistantMarkdown = memo(function AssistantMarkdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
    </div>
  );
});

export function MessageView({ message, live }: { message: Message; live: LiveRun | null }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl bg-panel px-4 py-2.5 leading-relaxed">
          {partText(message, "text")}
        </div>
      </div>
    );
  }

  const isLive = live !== null && live.messageId === message.id;
  const text = isLive ? live.text : partText(message, "text");
  const reasoning = isLive ? live.reasoning : partText(message, "reasoning");
  const status = isLive ? live.status : message.status;
  const waiting = isLive && !text && ["queued", "running", "reconnecting"].includes(status);

  return (
    <div className="space-y-2">
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
          {status === "queued" ? "Waiting for the model…" : status === "reconnecting" ? "Reconnecting…" : null}
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
    </div>
  );
}
