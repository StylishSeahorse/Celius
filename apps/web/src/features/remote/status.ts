// Shared wording and colours for Celius Remote (docs/remote/).

export const STATE_LABEL: Record<string, string> = {
  awaiting_authorization: "Awaiting your approval",
  revoked: "Revoked",
  needs_attention: "Needs attention",
  busy: "Busy",
  idle: "Online · idle",
  disconnected: "Disconnected unexpectedly",
  offline: "Offline",
};

export const STATE_DOT: Record<string, string> = {
  awaiting_authorization: "bg-amber-500",
  revoked: "bg-muted",
  needs_attention: "bg-amber-500",
  busy: "bg-sky-500",
  idle: "bg-emerald-500",
  disconnected: "bg-red-500",
  offline: "bg-muted",
};

export const SESSION_LABEL: Record<string, string> = {
  opening: "Opening on the machine…",
  idle: "Ready",
  running: "Working",
  awaiting_approval: "Waiting for your approval",
  failed: "Couldn't start",
  terminated: "Ended",
};

export const ADAPTER_LABEL: Record<string, string> = {
  celius: "Celius",
  claude_code: "Claude Code",
};

export const OS_LABEL: Record<string, string> = { windows: "Windows", linux: "Linux", macos: "macOS" };

export function osIcon(os: string | null | undefined): string {
  return os === "windows" ? "🪟" : os === "macos" ? "💻" : "🐧";
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export interface Resources {
  cpu_percent?: number | null;
  cpu_count?: number | null;
  memory?: { total_mb: number; available_mb: number; percent: number } | null;
  disks?: { path: string; total_gb: number; free_gb: number }[];
  gpus?: { name: string; utilization_percent: number; memory_used_mb: number; memory_total_mb: number }[];
  running_commands?: number;
  open_sessions?: number;
  load_average?: number[];
}

export function resourcesOf(value: unknown): Resources | null {
  return value && typeof value === "object" ? (value as Resources) : null;
}

export function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value)}%`;
}
