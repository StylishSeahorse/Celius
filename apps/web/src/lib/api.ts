import type {
  ConversationDetail,
  ConversationSummary,
  Me,
  ModelList,
  Run,
  SubmitResponse,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
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
    const error = body?.error ?? {};
    const detail = typeof body?.detail === "string" ? body.detail : undefined;
    throw new ApiError(
      response.status,
      error.code ?? `http_${response.status}`,
      error.message ?? detail ?? response.statusText,
    );
  }
  return body as T;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const api = {
  login: (username: string, password: string) =>
    request<Me>("/auth/login", json("POST", { username, password })),
  logout: () => request<void>("/auth/logout", json("POST")),
  me: () => request<Me>("/auth/me"),
  models: () => request<ModelList>("/models"),
  listConversations: (q?: string) =>
    request<{ items: ConversationSummary[]; next_cursor: string | null }>(
      `/conversations?limit=100${q ? `&q=${encodeURIComponent(q)}` : ""}`,
    ),
  createConversation: (body: { title?: string; model_override?: string | null }) =>
    request<ConversationDetail>("/conversations", json("POST", body)),
  getConversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
  updateConversation: (id: string, body: Record<string, unknown>) =>
    request<ConversationDetail>(`/conversations/${id}`, json("PATCH", body)),
  deleteConversation: (id: string) => request<void>(`/conversations/${id}`, json("DELETE")),
  sendMessage: (id: string, body: { client_message_id: string; content: string }) =>
    request<SubmitResponse>(`/conversations/${id}/messages`, json("POST", body)),
  cancelRun: (id: string) => request<Run>(`/runs/${id}/cancel`, json("POST")),
};
