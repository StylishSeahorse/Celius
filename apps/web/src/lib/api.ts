import type {
  ConversationDetail,
  ConversationSummary,
  Me,
  ModelList,
  Project,
  RegenerateResponse,
  Run,
  SubmitResponse,
  SyncChanges,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** Extra fields from the error body, e.g. `current` on a version conflict. */
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

/** True when the request never reached the server (offline, DNS, connection refused). */
export function isNetworkError(error: unknown): boolean {
  return error instanceof TypeError;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const { code, message, ...details } = body?.error ?? {};
    const detail = typeof body?.detail === "string" ? body.detail : undefined;
    throw new ApiError(
      response.status,
      code ?? `http_${response.status}`,
      message ?? detail ?? response.statusText,
      details,
    );
  }
  return body as T;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
});

export interface ConversationCreate {
  id?: string;
  title?: string;
  model_override?: string | null;
  project_id?: string | null;
}

export const api = {
  login: (username: string, password: string) =>
    request<Me>("/auth/login", json("POST", { username, password })),
  logout: () => request<void>("/auth/logout", json("POST")),
  me: () => request<Me>("/auth/me"),
  models: () => request<ModelList>("/models"),

  listConversations: (opts: { q?: string; projectId?: string | null } = {}) => {
    const params = new URLSearchParams({ limit: "200" });
    if (opts.q) params.set("q", opts.q);
    if (opts.projectId) params.set("project_id", opts.projectId);
    return request<{ items: ConversationSummary[]; next_cursor: string | null }>(
      `/conversations?${params}`,
    );
  },
  createConversation: (body: ConversationCreate) =>
    request<ConversationDetail>("/conversations", json("POST", body)),
  getConversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
  updateConversation: (id: string, body: Record<string, unknown>) =>
    request<ConversationDetail>(`/conversations/${id}`, json("PATCH", body)),
  deleteConversation: (id: string) => request<void>(`/conversations/${id}`, json("DELETE")),
  sendMessage: (id: string, body: { client_message_id: string; content: string }) =>
    request<SubmitResponse>(`/conversations/${id}/messages`, json("POST", body)),

  editMessage: (id: string, body: { client_message_id: string; content: string }) =>
    request<SubmitResponse>(`/messages/${id}/revisions`, json("POST", body)),
  regenerate: (id: string, body: { client_request_id: string }) =>
    request<RegenerateResponse>(`/messages/${id}/regenerate`, json("POST", body)),
  selectMessage: (id: string) =>
    request<ConversationDetail>(`/messages/${id}/select`, json("POST")),
  cancelRun: (id: string) => request<Run>(`/runs/${id}/cancel`, json("POST")),

  listProjects: () => request<Project[]>("/projects"),
  createProject: (body: { id?: string; name: string; instructions?: string | null }) =>
    request<Project>("/projects", json("POST", body)),
  updateProject: (id: string, body: Record<string, unknown>) =>
    request<Project>(`/projects/${id}`, json("PATCH", body)),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, json("DELETE")),

  syncCursor: () => request<{ cursor: number }>("/sync/cursor"),
  syncChanges: (cursor: number) => request<SyncChanges>(`/sync/changes?cursor=${cursor}`),
  syncAck: (device_id: string, cursor: number) =>
    request<void>("/sync/ack", json("POST", { device_id, cursor })),
};
