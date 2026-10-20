import { useCallback, useEffect, useState } from "react";

import { ApiError, type Invitation, type Person, api } from "../../lib/api";
import { formatBytes } from "../../lib/uploads";

const button = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-panel disabled:opacity-40";
const input = "w-full rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent";
const PROFILES = ["read_only", "standard", "developer", "administrator"];
const GB = 1024 ** 3;

/** People and invitations, for administrators (A-01..A-03). */
export function People({ meId }: { meId: string }) {
  const [people, setPeople] = useState<Person[] | null>(null);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [created, setCreated] = useState<Invitation | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(() => {
    void api.people().then(setPeople).catch(() => setPeople([]));
    void api.invitations().then(setInvitations).catch(() => setInvitations([]));
  }, []);
  useEffect(load, [load]);

  async function act(action: () => Promise<unknown>, done?: string) {
    setMessage(null);
    try {
      await action();
      if (done) setMessage(done);
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : "That didn't work");
    }
    load();
  }

  return (
    <section className="space-y-3" aria-label="People">
      <h3 className="font-medium">People</h3>
      <ul className="divide-y divide-line rounded-lg border border-line">
        {people?.map((p) => (
          <li key={p.id} className="space-y-1 px-3 py-2" data-testid="person" data-username={p.username}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{p.display_name || p.username}</span>
              {p.display_name && <span className="text-xs text-muted">@{p.username}</span>}
              {p.is_admin && <span className="rounded bg-accent/15 px-1.5 text-xs">admin</span>}
              {!p.is_active && <span className="rounded bg-red-500/15 px-1.5 text-xs">disabled</span>}
              <span className="ml-auto text-xs text-muted">
                {formatBytes(p.storage_used_bytes)}
                {p.last_seen_at ? ` · seen ${new Date(p.last_seen_at).toLocaleDateString()}` : ""}
              </span>
            </div>
            {p.id !== meId && (
              <div className="flex flex-wrap gap-1">
                <button
                  className={button}
                  onClick={() => void act(() => api.updatePerson(p.id, { is_active: !p.is_active }))}
                >
                  {p.is_active ? "Disable" : "Enable"}
                </button>
                <button className={button} onClick={() => setEditing(editing === p.id ? null : p.id)}>
                  Limits…
                </button>
                <button
                  className={button}
                  onClick={() => {
                    const password = window.prompt(`New password for ${p.username} (12+ characters)`);
                    if (password) void act(() => api.resetPersonPassword(p.id, password), "Password changed; they were signed out.");
                  }}
                >
                  Reset password
                </button>
                <button
                  className={button}
                  onClick={() => void act(() => api.updatePerson(p.id, { is_admin: !p.is_admin }))}
                >
                  {p.is_admin ? "Remove admin" : "Make admin"}
                </button>
                <button
                  className={`${button} text-red-600 dark:text-red-400`}
                  onClick={() => {
                    const typed = window.prompt(
                      `This deletes ${p.username} and all their chats, files and memories. Type their username to confirm.`,
                    );
                    if (typed) void act(() => api.deletePerson(p.id, typed), `${p.username} was deleted.`);
                  }}
                >
                  Delete…
                </button>
              </div>
            )}
            {editing === p.id && <Limits person={p} onSave={(body) => act(() => api.updatePerson(p.id, body), "Saved.")} />}
          </li>
        ))}
      </ul>

      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <button
            className="rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel"
            onClick={() =>
              void act(async () => {
                setCreated(await api.createInvitation({}));
              })
            }
          >
            Invite someone
          </button>
          <span className="text-xs text-muted">A one-time link, valid for 3 days.</span>
        </div>
        {created?.url && (
          <div className="space-y-1 rounded-lg border border-accent/40 bg-accent/5 p-2 text-xs">
            <p>Send this link to the person you're inviting. It is shown only now:</p>
            <input readOnly value={created.url} aria-label="Invitation link" className={input} onFocus={(e) => e.currentTarget.select()} />
            <button className={button} onClick={() => void navigator.clipboard?.writeText(created.url!)}>
              Copy
            </button>
          </div>
        )}
        {invitations.filter((i) => i.state === "open").length > 0 && (
          <ul className="space-y-1 text-xs">
            {invitations
              .filter((i) => i.state === "open")
              .map((i) => (
                <li key={i.id} className="flex items-center gap-2">
                  <span className="text-muted">
                    Open invitation{i.note ? ` (${i.note})` : ""}, until {new Date(i.expires_at).toLocaleDateString()}
                  </span>
                  <button className={button} onClick={() => void act(() => api.revokeInvitation(i.id))}>
                    Revoke
                  </button>
                </li>
              ))}
          </ul>
        )}
      </div>
      {message && (
        <p role="status" className="text-xs">
          {message}
        </p>
      )}
    </section>
  );
}

function Limits({ person, onSave }: { person: Person; onSave: (body: Partial<Person>) => void }) {
  const [quota, setQuota] = useState(person.storage_quota_bytes === null ? "" : String(person.storage_quota_bytes / GB));
  const [daily, setDaily] = useState(person.daily_message_limit === null ? "" : String(person.daily_message_limit));
  const [models, setModels] = useState((person.allowed_models ?? []).join(", "));
  const [profiles, setProfiles] = useState<string[] | null>(person.allowed_tool_profiles);

  return (
    <form
      aria-label={`Limits for ${person.username}`}
      className="grid gap-2 rounded-lg bg-panel/50 p-2 text-xs sm:grid-cols-2"
      onSubmit={(e) => {
        e.preventDefault();
        onSave({
          storage_quota_bytes: quota.trim() ? Math.round(Number(quota) * GB) : null,
          daily_message_limit: daily.trim() ? Number(daily) : null,
          allowed_models: models.trim() ? models.split(",").map((m) => m.trim()).filter(Boolean) : null,
          allowed_tool_profiles: profiles,
        });
      }}
    >
      <label className="space-y-0.5">
        <span className="text-muted">Storage (GB; empty = server default)</span>
        <input aria-label="Storage limit in GB" inputMode="decimal" value={quota} onChange={(e) => setQuota(e.target.value)} className={input} />
      </label>
      <label className="space-y-0.5">
        <span className="text-muted">Replies per 24 hours (empty = no limit)</span>
        <input aria-label="Replies per 24 hours" inputMode="numeric" value={daily} onChange={(e) => setDaily(e.target.value)} className={input} />
      </label>
      <label className="space-y-0.5 sm:col-span-2">
        <span className="text-muted">Models they may use, comma-separated (empty = all)</span>
        <input aria-label="Allowed models" value={models} onChange={(e) => setModels(e.target.value)} className={input} />
      </label>
      <fieldset className="space-y-0.5 sm:col-span-2">
        <legend className="text-muted">Tool profiles (none ticked = all; "read only" is always allowed)</legend>
        <div className="flex flex-wrap gap-3">
          {PROFILES.map((profile) => (
            <label key={profile} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={profiles?.includes(profile) ?? false}
                onChange={(e) => {
                  const next = new Set(profiles ?? []);
                  if (e.target.checked) next.add(profile);
                  else next.delete(profile);
                  setProfiles(next.size ? PROFILES.filter((p) => next.has(p)) : null);
                }}
              />
              {profile.replace("_", " ")}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="sm:col-span-2">
        <button className={button}>Save limits</button>
      </div>
    </form>
  );
}
