// API types come from the generated client (packages/api-client). Only the SSE event
// envelope lives here; it is defined by packages/contracts, not by OpenAPI.

export type {
  ConversationDetail,
  ConversationSummary,
  ActivityItem,
  ExportJob,
  FileKind,
  McpServer,
  PendingApproval,
  Schedule,
  ScheduleCreate,
  ToolCall,
  ToolCallStatus,
  ToolInfo,
  ToolProfile,
  ToolSettings,
  StorageUsage,
  StoredFile,
  UploadSession,
  Me,
  Message,
  MessagePart,
  MessageStatus,
  ModelInfo,
  ModelList,
  Project,
  RegenerateResponse,
  Run,
  RunStatus,
  SubmitResponse,
  SyncChange,
  SyncChanges,
} from "@celius/api-client";

export type MessageRole = "system" | "user" | "assistant" | "tool";

export type RunEventType =
  | "run.started"
  | "message.delta"
  | "tool_call.proposed"
  | "tool_call.awaiting_approval"
  | "tool_call.decided"
  | "tool_call.started"
  | "tool_call.completed"
  | "run.progress"
  | "run.completed"
  | "run.failed"
  | "run.cancelled";

export interface RunEvent {
  event_id: number;
  run_id: string;
  type: RunEventType;
  created_at: string;
  data: Record<string, unknown>;
}

export const TERMINAL_EVENTS: ReadonlySet<RunEventType> = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
]);
