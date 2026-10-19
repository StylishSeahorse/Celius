// Mirrors services/api/app/schemas. Replaced by the generated client in phase 3 (S-01).

export type MessageRole = "system" | "user" | "assistant" | "tool";
export type MessageStatus = "pending" | "streaming" | "complete" | "failed" | "cancelled";
export type RunStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export interface Me {
  user_id: string;
  username: string;
  display_name: string | null;
  is_admin: boolean;
}

export interface ConversationSummary {
  id: string;
  title: string | null;
  model_override: string | null;
  version: number;
  archived: boolean;
  updated_at: string;
}

export interface MessagePart {
  type: "text" | "reasoning" | string;
  text: string | null;
}

export interface Message {
  id: string;
  parent_id: string | null;
  role: MessageRole;
  status: MessageStatus;
  client_message_id: string | null;
  model_id: string | null;
  parts: MessagePart[];
  created_at: string;
}

export interface Run {
  id: string;
  status: RunStatus;
  output_message_id: string | null;
  model_id: string;
  error_code: string | null;
  error_message: string | null;
}

export interface ConversationDetail extends ConversationSummary {
  messages: Message[];
  active_run: Run | null;
}

export interface SubmitResponse {
  message: Message;
  assistant_message: Message;
  run: Run;
  conversation_version: number;
  duplicate: boolean;
}

export interface ModelInfo {
  id: string;
  context_tokens: number | null;
  capabilities: { tool_calling: boolean; vision: boolean; source: string };
}

export interface ModelList {
  default_model_id: string;
  models: ModelInfo[];
  health: { ok: boolean; detail: string | null; latency_ms: number | null };
}

export type RunEventType =
  | "run.started"
  | "message.delta"
  | "tool_call.proposed"
  | "tool_call.awaiting_approval"
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
