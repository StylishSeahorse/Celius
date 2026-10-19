import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import type { ToolProfile } from "../../lib/types";

let cached: Promise<ToolProfile[]> | null = null;

/** Profiles change rarely; one request per page load (reset after edits in settings). */
export function loadProfiles(fresh = false): Promise<ToolProfile[]> {
  if (!cached || fresh) cached = api.toolProfiles().catch(() => ((cached = null), []));
  return cached;
}

let defaultKey: Promise<string | null> | null = null;

/** The profile "default" stands for (the user's setting, else the server's). */
export function loadDefaultProfile(fresh = false): Promise<string | null> {
  if (!defaultKey || fresh)
    defaultKey = api.toolSettings().then((s) => s.default_profile, () => ((defaultKey = null), null));
  return defaultKey;
}

export function useToolProfiles(): ToolProfile[] {
  const [profiles, setProfiles] = useState<ToolProfile[]>([]);
  useEffect(() => {
    let alive = true;
    void loadProfiles().then((p) => alive && setProfiles(p));
    const onChange = () => void loadProfiles(true).then((p) => alive && setProfiles(p));
    window.addEventListener("celius:profiles-changed", onChange);
    return () => {
      alive = false;
      window.removeEventListener("celius:profiles-changed", onChange);
    };
  }, []);
  return profiles;
}

export function ToolProfilePicker({
  value,
  disabled,
  onChange,
}: {
  value: string | null;
  disabled?: boolean;
  onChange: (profile: string | null) => void;
}) {
  const profiles = useToolProfiles();
  const [fallback, setFallback] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = (fresh: boolean) => void loadDefaultProfile(fresh).then((k) => alive && setFallback(k));
    load(false);
    const onChange = () => load(true);
    window.addEventListener("celius:profiles-changed", onChange);
    return () => {
      alive = false;
      window.removeEventListener("celius:profiles-changed", onChange);
    };
  }, []);
  if (!profiles.length) return null;
  const current = profiles.find((p) => p.key === (value ?? fallback));
  const defaultLabel = profiles.find((p) => p.key === fallback)?.label;
  return (
    <select
      value={value ?? ""}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value || null)}
      aria-label="Tool permissions"
      title={current?.description ?? "What tools may do in this chat without asking"}
      className="min-w-0 max-w-44 truncate rounded-md border border-line bg-canvas px-2 py-1 text-sm max-sm:max-w-none max-sm:flex-1"
    >
      <option value="">Tools: default{defaultLabel ? ` (${defaultLabel})` : ""}</option>
      {profiles.map((p) => (
        <option key={p.key} value={p.key}>
          Tools: {p.label}
        </option>
      ))}
    </select>
  );
}
