// Types generated from the API's OpenAPI document. Regenerate with `pnpm gen:api`;
// CI fails if this package is stale relative to the API.
import type { components, paths } from "./schema";

export type { components, paths };

type Schemas = components["schemas"];

export type ConversationSummary = Schemas["ConversationSummary"];
export type ConversationDetail = Schemas["ConversationDetail"];
export type ConversationList = Schemas["ConversationList"];
export type Message = Schemas["MessageOut"];
export type MessagePart = Schemas["MessagePartOut"];
export type Run = Schemas["RunOut"];
export type SubmitResponse = Schemas["MessageSubmitResponse"];
export type RegenerateResponse = Schemas["RegenerateResponse"];
export type Project = Schemas["ProjectOut"];
export type ModelList = Schemas["ModelListResponse"];
export type ModelInfo = Schemas["ModelInfo"];
export type Me = Schemas["MeResponse"];
export type SyncChanges = Schemas["SyncChanges"];
export type SyncChange = Schemas["SyncChange"];
export type MessageStatus = Schemas["MessageStatus"];
export type RunStatus = Schemas["RunStatus"];
export type StoredFile = Schemas["FileOut"];
export type FileKind = Schemas["FileKind"];
export type FileStatus = Schemas["FileStatus"];
export type StorageUsage = Schemas["StorageUsage"];
export type UploadSession = Schemas["UploadOut"];
export type ExportJob = Schemas["ExportOut"];
