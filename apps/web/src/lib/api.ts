import type {
  ActivityItem,
  ConversationDetail,
  ConversationMemory,
  ConversationSummary,
  ExportJob,
  StorageUsage,
  StoredFile,
  UploadSession,
  McpServer,
  Me,
  Memory,
  MemorySettings,
  ModelList,
  PendingApproval,
  Schedule,
  ScheduleCreate,
  ToolCall,
  ToolInfo,
  ToolProfile,
  ToolSettings,
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
  tool_profile?: string | null;
  memory_enabled?: boolean;
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
  checkInvitation: (token: string) =>
    request<{ expires_at: string; note: string | null }>(`/auth/invitations/${encodeURIComponent(token)}`),
  register: (body: { token: string; username: string; password: string; display_name?: string | null }) =>
    request<{ access_token: string }>("/auth/register", json("POST", body)),
  people: () => request<Person[]>("/admin/users"),
  updatePerson: (id: string, body: Partial<Omit<Person, "id" | "username" | "created_at">>) =>
    request<Person>(`/admin/users/${id}`, json("PATCH", body)),
  resetPersonPassword: (id: string, newPassword: string) =>
    request<void>(`/admin/users/${id}/password`, json("POST", { new_password: newPassword })),
  deletePerson: (id: string, username: string) =>
    request<{ files: number; conversations: number }>(
      `/admin/users/${id}?confirm=${encodeURIComponent(username)}`,
      json("DELETE"),
    ),
  invitations: () => request<Invitation[]>("/admin/invitations"),
  createInvitation: (body: { note?: string | null; make_admin?: boolean; expires_hours?: number }) =>
    request<Invitation>("/admin/invitations", json("POST", body)),
  revokeInvitation: (id: string) => request<void>(`/admin/invitations/${id}`, json("DELETE")),
  projectMembers: (projectId: string) => request<ProjectMember[]>(`/projects/${projectId}/members`),
  shareProject: (projectId: string, username: string, role: "read" | "write") =>
    request<ProjectMember>(`/projects/${projectId}/members/${encodeURIComponent(username)}`, json("PUT", { role })),
  unshareProject: (projectId: string, username: string) =>
    request<void>(`/projects/${projectId}/members/${encodeURIComponent(username)}`, json("DELETE")),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/auth/password", json("POST", { current_password, new_password })),
  sessions: () => request<unknown[]>("/auth/sessions"),
  revokeOtherSessions: () => request<void>("/auth/sessions/revoke-others", json("POST")),
  pushInfo: (endpoint?: string) =>
    request<{ enabled: boolean; public_key: string; devices: number; subscribed: boolean }>(
      `/push${endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : ""}`,
    ),
  pushSubscribe: (subscription: PushSubscriptionJSON) =>
    request<void>("/push/subscriptions", json("POST", subscription)),
  pushUnsubscribe: (endpoint: string) => request<void>("/push/subscriptions/remove", json("POST", { endpoint })),
  pushTest: () => request<{ queued: boolean }>("/push/test", json("POST")),
  status: () =>
    request<{ status: string; checks: unknown[]; alerts: unknown }>("/status"),
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
  deleteConversation: (id: string, forgetMemories = false) =>
    request<void>(`/conversations/${id}${forgetMemories ? "?forget_memories=true" : ""}`, json("DELETE")),
  conversationMemory: (id: string) => request<ConversationMemory>(`/conversations/${id}/memory`),
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
  uploadProgress: (id: string) =>
    request<{ received_bytes: number; declared_size: number; status: string }>(`/files/uploads/${id}`),
  getFile: (id: string) => request<StoredFile>(`/files/${id}`),
  listFiles: (projectId: string) => request<StoredFile[]>(`/files?project_id=${projectId}`),
  deleteFile: (id: string) => request<void>(`/files/${id}`, json("DELETE")),
  storageUsage: () => request<StorageUsage>("/files/usage"),
  createExport: (body: { scope: "conversation" | "project"; id: string; format: "markdown" | "json" | "zip" }) =>
    request<ExportJob>("/exports", json("POST", body)),
  getExport: (id: string) => request<ExportJob>(`/exports/${id}`),

  tools: () => request<ToolInfo[]>("/tools"),
  toolSettings: () => request<ToolSettings>("/tools/settings"),
  updateToolSettings: (body: { default_profile?: string; disabled_tools?: string[] }) =>
    request<ToolSettings>("/tools/settings", json("PUT", body)),
  toolProfiles: () => request<ToolProfile[]>("/tool-profiles"),
  createToolProfile: (body: { name: string; base: string; description?: string | null; rules: Record<string, string> }) =>
    request<ToolProfile>("/tool-profiles", json("POST", body)),
  updateToolProfile: (id: string, body: Record<string, unknown>) =>
    request<ToolProfile>(`/tool-profiles/${id}`, json("PATCH", body)),
  deleteToolProfile: (id: string) => request<void>(`/tool-profiles/${id}`, json("DELETE")),
  pendingApprovals: () => request<PendingApproval[]>("/tool-calls/pending"),
  /** scope: this call only, this tool for the rest of the chat, or every tool in the chat. */
  approveToolCall: (id: string, args_hash: string, scope: "once" | "tool" | "all" = "once") =>
    request<ToolCall>(
      `/tool-calls/${id}/approve`,
      json("POST", { args_hash, remember: scope === "tool", remember_all: scope === "all" }),
    ),
  denyToolCall: (id: string, reason?: string) =>
    request<ToolCall>(`/tool-calls/${id}/deny`, json("POST", { reason: reason ?? null })),
  clearGrants: (conversationId: string) =>
    request<void>(`/conversations/${conversationId}/tool-grants`, json("DELETE")),
  workspace: (conversationId: string) =>
    request<{ entries: { path: string; size: number; is_dir: boolean }[]; truncated: boolean }>(
      `/conversations/${conversationId}/workspace`,
    ),

  schedules: () => request<Schedule[]>("/schedules"),
  createSchedule: (body: ScheduleCreate) => request<Schedule>("/schedules", json("POST", body)),
  updateSchedule: (id: string, body: Record<string, unknown>) =>
    request<Schedule>(`/schedules/${id}`, json("PATCH", body)),
  deleteSchedule: (id: string) => request<void>(`/schedules/${id}`, json("DELETE")),
  runSchedule: (id: string) => request<Schedule>(`/schedules/${id}/run`, json("POST")),

  mcpServers: () => request<McpServer[]>("/mcp-servers"),
  createMcpServer: (body: { name: string; url: string; allow_private_network: boolean; headers?: Record<string, string> | null }) =>
    request<McpServer>("/mcp-servers", json("POST", body)),
  updateMcpServer: (id: string, body: Record<string, unknown>) =>
    request<McpServer>(`/mcp-servers/${id}`, json("PATCH", body)),
  refreshMcpServer: (id: string) => request<McpServer>(`/mcp-servers/${id}/refresh`, json("POST")),
  deleteMcpServer: (id: string) => request<void>(`/mcp-servers/${id}`, json("DELETE")),

  memorySettings: () => request<MemorySettings>("/memory/settings"),
  updateMemorySettings: (body: { enabled?: boolean; auto_extract?: boolean; require_review?: boolean }) =>
    request<MemorySettings>("/memory/settings", json("PUT", body)),
  memories: (status: "active" | "proposed", q?: string) =>
    request<Memory[]>(`/memories?status=${status}${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  createMemory: (body: {
    content: string;
    scope: "global" | "project";
    project_id?: string | null;
    always_use?: boolean;
  }) =>
    request<Memory>("/memories", json("POST", body)),
  updateMemory: (
    id: string,
    body: { content?: string; scope?: "global" | "project"; project_id?: string | null; always_use?: boolean },
  ) =>
    request<Memory>(`/memories/${id}`, json("PATCH", body)),
  approveMemory: (id: string, content?: string) =>
    request<Memory>(`/memories/${id}/approve`, json("POST", content ? { content } : {})),
  rejectMemory: (id: string) => request<void>(`/memories/${id}/reject`, json("POST")),
  deleteMemory: (id: string) => request<void>(`/memories/${id}`, json("DELETE")),
  forgetEverything: () =>
    request<{ memories: number; excerpts: number }>("/memories?confirm=forget-everything", json("DELETE")),

  activity: () => request<{ items: ActivityItem[] }>("/activity"),
  cancelActivity: (kind: "run" | "job", id: string) =>
    request<void>(`/activity/${kind}s/${id}/cancel`, json("POST")),

  syncCursor: () => request<{ cursor: number }>("/sync/cursor"),
  syncChanges: (cursor: number) => request<SyncChanges>(`/sync/changes?cursor=${cursor}`),
  syncAck: (device_id: string, cursor: number) =>
    request<void>("/sync/ack", json("POST", { device_id, cursor })),
};

export const workspaceUrl = (conversationId: string, path: string) =>
  `/api/v1/conversations/${conversationId}/workspace/file?path=${encodeURIComponent(path)}`;

export const fileUrls = {
  download: (id: string) => `/api/v1/files/${id}/download`,
  inline: (id: string) => `/api/v1/files/${id}/download?inline=1`,
  thumbnail: (id: string) => `/api/v1/files/${id}/thumbnail`,
};

export interface Person {
  id: string;
  username: string;
  display_name: string | null;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
  last_seen_at: string | null;
  storage_used_bytes: number;
  storage_quota_bytes: number | null;
  daily_message_limit: number | null;
  allowed_models: string[] | null;
  allowed_tool_profiles: string[] | null;
}

export interface Invitation {
  id: string;
  note: string | null;
  make_admin: boolean;
  state: "open" | "used" | "expired" | "revoked";
  expires_at: string;
  created_at: string;
  used_by: string | null;
  url: string | null;
}

export interface ProjectMember {
  username: string;
  display_name: string | null;
  role: "read" | "write";
}
