import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { newId } from "../../lib/ids";
import { followRun } from "../../lib/sse";
import type { ConversationDetail, Message, Run, RunEvent } from "../../lib/types";

export interface LiveRun {
  runId: string;
  messageId: string | null;
  text: string;
  reasoning: string;
  status: "queued" | "running" | "reconnecting" | "completed" | "failed" | "cancelled";
  error: string | null;
}

interface Options {
  conversationId: string | null;
  modelOverride: string | null;
  onConversationCreated: (id: string) => void;
  onChanged: () => void;
}

export function useChat({ conversationId, modelOverride, onConversationCreated, onChanged }: Options) {
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [live, setLive] = useState<LiveRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const streamAbort = useRef<AbortController | null>(null);
  const callbacks = useRef({ onChanged });
  callbacks.current = { onChanged };

  const reload = useCallback(async (id: string) => {
    const fresh = await api.getConversation(id);
    setDetail(fresh);
    return fresh;
  }, []);

  const follow = useCallback(
    (run: Run, conversation: string) => {
      streamAbort.current?.abort();
      const controller = new AbortController();
      streamAbort.current = controller;
      setLive({
        runId: run.id,
        messageId: run.output_message_id,
        text: "",
        reasoning: "",
        status: run.status === "queued" ? "queued" : "running",
        error: null,
      });
      const onEvent = (event: RunEvent) => setLive((current) => applyEvent(current, event));
      followRun(run.id, {
        signal: controller.signal,
        onEvent,
        onReconnecting: () =>
          setLive((current) => (current ? { ...current, status: "reconnecting" } : current)),
      })
        .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
        .finally(async () => {
          if (controller.signal.aborted) return;
          await reload(conversation).catch(() => undefined);
          setLive((current) =>
            current && current.runId === run.id && current.status !== "failed" ? null : current,
          );
          callbacks.current.onChanged();
        });
    },
    [reload],
  );

  // Load the selected conversation and attach to its in-progress run, if any.
  useEffect(() => {
    if (conversationId === null) {
      streamAbort.current?.abort();
      setDetail(null);
      setLive(null);
      setError(null);
      return;
    }
    if (detail?.id === conversationId) return; // just created locally
    streamAbort.current?.abort();
    setLive(null);
    setError(null);
    setLoading(true);
    reload(conversationId)
      .then((fresh) => {
        if (fresh.active_run) follow(fresh.active_run, fresh.id);
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [conversationId]);

  useEffect(() => () => streamAbort.current?.abort(), []);

  const send = useCallback(
    async (content: string) => {
      setSending(true);
      setError(null);
      try {
        let conversation = detail;
        if (conversation === null) {
          conversation = await api.createConversation({ model_override: modelOverride });
          setDetail(conversation);
          onConversationCreated(conversation.id);
        }
        const result = await api.sendMessage(conversation.id, {
          client_message_id: newId(),
          content,
        });
        setDetail((current) =>
          current
            ? {
                ...current,
                title: current.title ?? content.slice(0, 80),
                version: result.conversation_version,
                messages: appendUnique(current.messages, result.message, result.assistant_message),
                active_run: result.run,
              }
            : current,
        );
        follow(result.run, conversation.id);
        callbacks.current.onChanged();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Could not send the message");
        throw err;
      } finally {
        setSending(false);
      }
    },
    [detail, follow, modelOverride, onConversationCreated],
  );

  const cancel = useCallback(async () => {
    if (live) await api.cancelRun(live.runId).catch(() => undefined);
  }, [live]);

  const busy = sending || (live !== null && !["completed", "failed", "cancelled"].includes(live.status));
  return { detail, setDetail, live, loading, sending, busy, error, send, cancel };
}

function appendUnique(messages: Message[], ...added: Message[]): Message[] {
  const ids = new Set(messages.map((m) => m.id));
  return [...messages, ...added.filter((m) => !ids.has(m.id))];
}

export function applyEvent(current: LiveRun | null, event: RunEvent): LiveRun | null {
  if (!current || current.runId !== event.run_id) return current;
  const data = event.data as Record<string, string | undefined>;
  switch (event.type) {
    case "run.started":
      return { ...current, status: "running", messageId: data.message_id ?? current.messageId };
    case "message.delta":
      return {
        ...current,
        status: "running",
        text: current.text + (data.text ?? ""),
        reasoning: current.reasoning + (data.reasoning ?? ""),
      };
    case "run.progress":
      // The worker restarted generation from scratch; discard the partial output.
      return data.stage === "restarted" ? { ...current, text: "", reasoning: "", status: "queued" } : current;
    case "run.completed":
      return { ...current, status: "completed" };
    case "run.cancelled":
      return { ...current, status: "cancelled" };
    case "run.failed":
      return { ...current, status: "failed", error: data.message ?? "Generation failed" };
    default:
      return current;
  }
}
