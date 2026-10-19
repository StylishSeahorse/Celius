import type {
  ConversationDetail,
  ConversationSummary,
  ExportJob,
  StorageUsage,
  StoredFile,
  UploadSession,
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
  reasoning?: boolean | null;
}

export interface MessageBody {
  client_message_id: string;
  content: string;
  attachment_ids?: string[];
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
  sendMessage: (id: string, body: MessageBody) =>
    request<SubmitResponse>(`/conversations/${id}/messages`, json("POST", body)),

  editMessage: (id: string, body: MessageBody) =>
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

  createUpload: (body: {
    filename: string;
    size: number;
    relative_path?: string | null;
    project_id?: string | null;
  }) => request<UploadSession>("/files/uploads", json("POST", body)),
  completeUpload: (id: string) => request<StoredFile>(`/files/uploads/${id}/complete`, json("POST")),
  getFile: (id: string) => request<StoredFile>(`/files/${id}`),
  listFiles: (projectId: string) => request<StoredFile[]>(`/files?project_id=${projectId}`),
  deleteFile: (id: string) => request<void>(`/files/${id}`, json("DELETE")),
  storageUsage: () => request<StorageUsage>("/files/usage"),
  createExport: (body: { scope: "conversation" | "project"; id: string; format: "markdown" | "json" | "zip" }) =>
    request<ExportJob>("/exports", json("POST", body)),
  getExport: (id: string) => request<ExportJob>(`/exports/${id}`),

  syncCursor: () => request<{ cursor: number }>("/sync/cursor"),
  syncChanges: (cursor: number) => request<SyncChanges>(`/sync/changes?cursor=${cursor}`),
  syncAck: (device_id: string, cursor: number) =>
    request<void>("/sync/ack", json("POST", { device_id, cursor })),
};

export const fileUrls = {
  download: (id: string) => `/api/v1/files/${id}/download`,
  inline: (id: string) => `/api/v1/files/${id}/download?inline=1`,
  thumbnail: (id: string) => `/api/v1/files/${id}/thumbnail`,
};
