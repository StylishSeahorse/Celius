import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, isNetworkError } from "../../lib/api";
import { newId } from "../../lib/ids";
import { followRun } from "../../lib/sse";
import type { ConversationDetail, ConversationSummary, Run, RunEvent, ToolCall } from "../../lib/types";
import { cache } from "../../offline/db";
import { type OutboxItem, outbox } from "../../offline/outbox";

export interface LiveRun {
  runId: string;
  messageId: string | null;
  text: string;
  reasoning: string;
  status:
    | "queued"
    | "running"
    | "reconnecting"
    | "awaiting_approval"
    | "completed"
    | "failed"
    | "cancelled";
  error: string | null;
  /** Latest state of each tool call seen on the stream, by id. */
  toolCalls: Record<string, ToolCall>;
}

export interface Conflict {
  current: ConversationSummary;
  fields: Record<string, unknown>;
}

interface Options {
  conversationId: string | null;
  /** For a conversation started from this view (new chat). */
  draft: {
    modelOverride: string | null;
    projectId: string | null;
    reasoning: boolean | null;
    toolProfile: string | null;
  };
  onConversationCreated: (id: string) => void;
  onChanged: () => void;
}

const LIVE_STATUSES = ["queued", "running", "reconnecting", "awaiting_approval"];
// Events after which the persisted message changed (a step was saved, a call finished).
const RELOAD_EVENTS = new Set(["tool_call.proposed", "tool_call.completed", "tool_call.decided"]);

function placeholder(id: string, draft: Options["draft"], title: string): ConversationDetail {
  const now = new Date().toISOString();
  return {
    id,
    title,
    project_id: draft.projectId,
    model_override: draft.modelOverride,
    memory_enabled: true,
    reasoning: draft.reasoning,
    tool_profile: draft.toolProfile,
    active_branch_id: null,
    version: 0,
    archived: false,
    created_at: now,
    updated_at: now,
    messages: [],
    active_run: null,
  };
}

export function useChat({ conversationId, draft, onConversationCreated, onChanged }: Options) {
  const [detail, setDetailState] = useState<ConversationDetail | null>(null);
  const [live, setLive] = useState<LiveRun | null>(null);
  const [pending, setPending] = useState<OutboxItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [fromCache, setFromCache] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const streamAbort = useRef<AbortController | null>(null);
  const liveRef = useRef<LiveRun | null>(null);
  liveRef.current = live;
  const idRef = useRef(conversationId);
  idRef.current = conversationId;
  const callbacks = useRef({ onChanged });
  callbacks.current = { onChanged };

  const setDetail = setDetailState;

  // Keep the device copy current, so the conversation opens instantly and offline.
  // Placeholders for conversations not yet on the server (version 0) aren't saved.
  useEffect(() => {
    if (detail && detail.version > 0 && !fromCache) void cache.putDetail(detail);
  }, [detail, fromCache]);

  const follow = useCallback((run: Run, conversation: string) => {
    if (liveRef.current?.runId === run.id && LIVE_STATUSES.includes(liveRef.current.status)) return;
    streamAbort.current?.abort();
    const controller = new AbortController();
    streamAbort.current = controller;
    setLive({
      runId: run.id,
      messageId: run.output_message_id,
      text: "",
      reasoning: "",
      status:
        run.status === "queued" ? "queued" : run.status === "awaiting_approval" ? "awaiting_approval" : "running",
      error: null,
      toolCalls: {},
    });
    let reload: ReturnType<typeof setTimeout> | null = null;
    followRun(run.id, {
      signal: controller.signal,
      onEvent: (event) => {
        setLive((current) => applyEvent(current, event));
        // Saved steps and tool results show from the server's copy of the message.
        if (RELOAD_EVENTS.has(event.type) && !reload) {
          reload = setTimeout(() => {
            reload = null;
            if (idRef.current !== conversation) return;
            void api.getConversation(conversation).then((fresh) => {
              if (idRef.current === conversation && !controller.signal.aborted) setDetail(fresh);
            }).catch(() => undefined);
          }, 150);
        }
        if (event.type === "tool_call.awaiting_approval") callbacks.current.onChanged();
      },
      onReconnecting: () =>
        setLive((current) => (current ? { ...current, status: "reconnecting" } : current)),
    })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(async () => {
        if (controller.signal.aborted) return;
        if (idRef.current === conversation) {
          const fresh = await api.getConversation(conversation).catch(() => null);
          if (fresh && idRef.current === conversation) setDetail(fresh);
        }
        setLive((current) =>
          current && current.runId === run.id && current.status !== "failed" ? null : current,
        );
        callbacks.current.onChanged();
        void outbox.flush(); // queued messages for this conversation can go now
      });
  }, [setDetail]);

  /** Re-read the conversation from the server (sync, reconnect). Keeps a live stream. */
  const refresh = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;
    try {
      const fresh = await api.getConversation(id);
      if (idRef.current !== id) return;
      setDetail(fresh);
      setFromCache(false);
      if (fresh.active_run) follow(fresh.active_run, id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404 && !outbox.list(id).some((i) => i.create_conversation)) {
        setError("This conversation was deleted on another device.");
      }
    }
  }, [follow, setDetail]);

  // Pending (queued or failed) messages for this conversation.
  useEffect(() => {
    const update = () => setPending(conversationId ? outbox.list(conversationId) : []);
    update();
    return outbox.subscribe((event) => {
      update();
      if (event.type === "sent" && event.item.conversation_id === idRef.current) {
        const id = event.item.conversation_id;
        void api.getConversation(id).then((fresh) => {
          if (idRef.current === id) setDetail(fresh);
        }).catch(() => undefined);
        follow(event.response.run, id);
        callbacks.current.onChanged();
      }
    });
  }, [conversationId, follow, setDetail]);

  // Load the selected conversation: cached copy first, then the server.
  useEffect(() => {
    setError(null);
    if (conversationId === null) {
      streamAbort.current?.abort();
      setDetailState(null);
      setLive(null);
      return;
    }
    if (detail?.id === conversationId) return; // just created on this device
    streamAbort.current?.abort();
    setLive(null);
    setDetailState(null);
    setLoading(true);
    let cancelled = false;
    void (async () => {
      const cached = await cache.detail(conversationId);
      if (cached && !cancelled) {
        setDetailState(cached);
        setFromCache(true);
        setLoading(false);
      }
      try {
        const fresh = await api.getConversation(conversationId);
        if (cancelled) return;
        setDetail(fresh);
        setFromCache(false);
        if (fresh.active_run) follow(fresh.active_run, fresh.id);
      } catch (err) {
        if (cancelled) return;
        const queuedCreate = outbox.list(conversationId).find((i) => i.create_conversation);
        if (queuedCreate) {
          const created = queuedCreate.create_conversation!;
          setDetailState(
            placeholder(
              conversationId,
              {
                modelOverride: created.model_override,
                projectId: created.project_id,
                reasoning: created.reasoning ?? null,
                toolProfile: created.tool_profile ?? null,
              },
              queuedCreate.content.slice(0, 80) || queuedCreate.attachments?.[0]?.filename || "New chat",
            ),
          );
        } else if (isNetworkError(err)) {
          if (!cached) setError("You're offline and this conversation isn't saved on this device.");
        } else {
          setError(err instanceof ApiError ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Deliberately keyed on the id only: reload when another conversation is opened.
  }, [conversationId]);

  useEffect(() => () => streamAbort.current?.abort(), []);

  const send = useCallback(
    async (content: string, attachments: { id: string; filename: string }[] = []) => {
      setError(null);
      let id = detail?.id;
      let create: OutboxItem["create_conversation"];
      if (!id) {
        id = newId();
        create = {
          model_override: draft.modelOverride,
          project_id: draft.projectId,
          reasoning: draft.reasoning,
          tool_profile: draft.toolProfile,
        };
        setDetailState(placeholder(id, draft, content.slice(0, 80) || attachments[0]?.filename || "New chat"));
        onConversationCreated(id);
      }
      await outbox.enqueue({
        client_message_id: newId(),
        conversation_id: id,
        content,
        ...(attachments.length ? { attachments } : {}),
        ...(create ? { create_conversation: create } : {}),
      });
    },
    [detail?.id, draft, onConversationCreated],
  );

  const cancel = useCallback(async () => {
    if (live) await api.cancelRun(live.runId).catch(() => undefined);
  }, [live]);

  /** Run a branch operation (online only) and show its result. */
  const branchOp = useCallback(
    async (op: () => Promise<{ run?: Run } | ConversationDetail>) => {
      if (!detail) return;
      setError(null);
      try {
        const result = await op();
        if ("messages" in result) {
          setDetail(result);
        } else {
          const fresh = await api.getConversation(detail.id);
          setDetail(fresh);
          if (result.run) follow(result.run, detail.id);
        }
        callbacks.current.onChanged();
      } catch (err) {
        setError(
          isNetworkError(err)
            ? "You're offline. Editing, regenerating and switching versions need a connection."
            : err instanceof ApiError ? err.message : String(err),
        );
      }
    },
    [detail, follow, setDetail],
  );

  const regenerate = useCallback(
    (messageId: string) => branchOp(() => api.regenerate(messageId, { client_request_id: newId() })),
    [branchOp],
  );
  const edit = useCallback(
    (messageId: string, content: string, attachmentIds: string[] = []) =>
      branchOp(() =>
        api.editMessage(messageId, { client_message_id: newId(), content, attachment_ids: attachmentIds }),
      ),
    [branchOp],
  );
  const selectSibling = useCallback(
    (messageId: string) => branchOp(() => api.selectMessage(messageId)),
    [branchOp],
  );

  /**
   * Change conversation settings without silently overwriting another device's change.
   * Returns a conflict for the UI to resolve ("keep theirs" / "use mine").
   */
  const update = useCallback(
    async (fields: Record<string, unknown>, expectedVersion?: number): Promise<Conflict | null> => {
      if (!detail) return null;
      try {
        const updated = await api.updateConversation(detail.id, {
          ...fields,
          expected_version: expectedVersion ?? detail.version,
        });
        setDetail((current) => (current ? { ...current, ...summaryOf(updated) } : current));
        callbacks.current.onChanged();
        return null;
      } catch (err) {
        if (err instanceof ApiError && err.code === "version_conflict") {
          return { current: err.details.current as ConversationSummary, fields };
        }
        setError(err instanceof ApiError ? err.message : "Could not save the change");
        return null;
      }
    },
    [detail, setDetail],
  );

  const busy = live !== null && LIVE_STATUSES.includes(live.status);
  return {
    detail,
    setDetail,
    live,
    pending,
    loading,
    fromCache,
    busy,
    error,
    send,
    cancel,
    regenerate,
    edit,
    selectSibling,
    update,
    refresh,
  };
}

function summaryOf(detail: ConversationDetail): Partial<ConversationDetail> {
  const { messages: _m, active_run: _r, ...summary } = detail;
  return summary;
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
    case "tool_call.proposed": {
      // The step's text is now saved on the message; the stream continues with the next.
      const call = (event.data as { tool_call?: ToolCall }).tool_call;
      return {
        ...current,
        text: "",
        reasoning: "",
        toolCalls: call ? { ...current.toolCalls, [call.id]: call } : current.toolCalls,
      };
    }
    case "tool_call.awaiting_approval":
    case "tool_call.decided":
    case "tool_call.started":
    case "tool_call.completed": {
      const call = (event.data as { tool_call?: ToolCall }).tool_call;
      return call ? { ...current, toolCalls: { ...current.toolCalls, [call.id]: call } } : current;
    }
    case "run.progress":
      if (data.stage === "restarted") {
        // The worker restarted this step from scratch; discard its partial output.
        return { ...current, text: "", reasoning: "", status: "queued" };
      }
      if (data.stage === "awaiting_approval") return { ...current, status: "awaiting_approval" };
      if (data.stage === "resumed") return { ...current, status: "running" };
      return current;
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
