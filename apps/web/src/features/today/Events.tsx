import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { EventSettings, EventSource, EventSuggestion, IntegrationAccount } from "../../lib/types";

const small = "rounded-md border border-line px-2 py-0.5 text-xs hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";
const input = "rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent";

function message(err: unknown) {
  return err instanceof ApiError ? err.message : String(err);
}

function when(e: EventSuggestion) {
  const start = new Date(e.starts_at);
  const day = start.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  if (e.all_day) return day;
  const time = start.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const end = e.ends_at ? new Date(e.ends_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : null;
  return `${day}, ${time}${end ? `–${end}` : ""}`;
}

/** Events found in venues' and businesses' posts, waiting to be added (ADR C-17). */
export function EventsView() {
  const [settings, setSettings] = useState<EventSettings | null>(null);
  const [sources, setSources] = useState<EventSource[]>([]);
  const [found, setFound] = useState<EventSuggestion[]>([]);
  const [calendars, setCalendars] = useState<IntegrationAccount[]>([]);
  const [form, setForm] = useState({ platform: "instagram" as "instagram" | "facebook", handle: "", label: "" });
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    void Promise.all([api.eventSettings(), api.eventSources(), api.eventSuggestions(), api.accounts()]).then(
      ([s, list, suggestions, accounts]) => {
        setSettings(s);
        setSources(list);
        setFound(suggestions);
        setCalendars(accounts.filter((a) => a.kind === "calendar"));
      },
      (err) => setError(message(err)),
    );
  }, []);
  useEffect(load, [load]);

  useEffect(() => {
    // Back from Meta: "#/today?tab=events&meta=connected&message=@account".
    const query = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
    const outcome = query.get("meta");
    if (outcome === "connected") setNotice(`Connected Instagram ${query.get("message") ?? ""}`);
    else if (outcome === "cancelled") setNotice("Signing in with Meta was cancelled");
    else if (outcome) setError(query.get("message") || "Signing in with Meta failed");
    if (query.has("tab") || outcome) window.history.replaceState(null, "", "#/today");
  }, []);

  // While a scan runs, check back now and then.
  useEffect(() => {
    if (!settings?.scanning) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [settings?.scanning, load]);

  function addSource(event: FormEvent) {
    event.preventDefault();
    setError(null);
    api
      .addEventSource({ platform: form.platform, handle: form.handle, ...(form.label ? { label: form.label } : {}) })
      .then(() => {
        setForm((f) => ({ ...f, handle: "", label: "" }));
        load();
      }, (err) => setError(message(err)));
  }

  async function act(id: string, work: () => Promise<unknown>) {
    setBusy(id);
    setError(null);
    try {
      await work();
      setFound((list) => list.filter((e) => e.id !== id));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(null);
    }
  }

  if (!settings) return error ? <p role="alert">{error}</p> : <p className="text-muted">Loading…</p>;
  const calendar = settings.calendar ?? "local";

  return (
    <div className="space-y-5 text-sm">
      {notice && <p role="status" className="text-xs text-green-700 dark:text-green-400">{notice}</p>}
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}

      <section className="space-y-2" aria-label="Found events">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="flex-1 font-medium">Found events {found.length > 0 && <span className="text-muted">({found.length})</span>}</h3>
          <span className="text-xs text-muted">
            {settings.scanning
              ? "Scanning…"
              : settings.scanned_at
                ? `Last scan ${new Date(settings.scanned_at).toLocaleDateString()}`
                : "Not scanned yet"}
          </span>
          <button
            className={small}
            disabled={settings.scanning || sources.length === 0}
            onClick={() => void api.scanEvents().then(load, (err) => setError(message(err)))}
          >
            Scan now
          </button>
        </div>
        {found.length === 0 ? (
          <p className="text-muted">Nothing waiting. New events show here after each scan.</p>
        ) : (
          <ul className="space-y-2">
            {found.map((e) => (
              <li key={e.id} className="flex gap-3 rounded-lg border border-line p-2" data-testid="event-suggestion">
                {e.image_url && (
                  <img
                    src={e.image_url}
                    alt=""
                    loading="lazy"
                    referrerPolicy="no-referrer"
                    className="h-20 w-20 shrink-0 rounded object-cover"
                    onError={(ev) => (ev.currentTarget.style.display = "none")}
                  />
                )}
                <div className="min-w-0 flex-1 space-y-0.5">
                  <p className="font-medium">{e.title}</p>
                  <p className="text-xs">
                    {when(e)}
                    {e.venue ? ` · ${e.venue}` : ""}
                    {e.price ? ` · ${e.price}` : ""}
                  </p>
                  {e.summary && <p className="text-xs text-muted">{e.summary}</p>}
                  <p className="flex flex-wrap gap-x-3 text-xs">
                    {e.post_url && (
                      <a className="underline" href={e.post_url} target="_blank" rel="noreferrer noopener">
                        Post{e.source ? ` (${e.source})` : ""}
                      </a>
                    )}
                    {e.ticket_url && (
                      <a className="underline" href={e.ticket_url} target="_blank" rel="noreferrer noopener">
                        Tickets
                      </a>
                    )}
                    {e.also_posted.length > 0 && <span className="text-muted">also posted by {e.also_posted.length} more</span>}
                  </p>
                  <div className="flex gap-2 pt-1">
                    <button
                      className={primary}
                      disabled={busy === e.id}
                      onClick={() => void act(e.id, () => api.addEventSuggestion(e.id, calendar))}
                    >
                      Add to calendar
                    </button>
                    <button className={small} disabled={busy === e.id} onClick={() => void act(e.id, () => api.dismissEventSuggestion(e.id))}>
                      Dismiss
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-2" aria-label="Event finder settings">
        <h3 className="font-medium">Settings</h3>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1">
            Check every
            <select
              className={input}
              aria-label="Check every"
              value={settings.every_days}
              onChange={(ev) => void api.updateEventSettings({ every_days: Number(ev.target.value) }).then(setSettings)}
            >
              {[1, 2, 3, 4, 7, 14].map((n) => (
                <option key={n} value={n}>
                  {n} {n === 1 ? "day" : "days"}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1">
            Add to
            <select
              className={input}
              aria-label="Add to calendar"
              value={calendar}
              onChange={(ev) => void api.updateEventSettings({ calendar: ev.target.value }).then(setSettings)}
            >
              <option value="local">Celius calendar</option>
              {calendars.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        {settings.meta_configured ? (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs">
              {settings.meta_connected ? `Instagram: reading as ${settings.meta_account}` : "Instagram: not connected"}
              {settings.meta_error && <span className="block text-amber-700 dark:text-amber-300">{settings.meta_error}</span>}
            </span>
            <button className={small} onClick={() => void api.metaStart().then(({ url }) => window.location.assign(url), (err) => setError(message(err)))}>
              {settings.meta_connected ? "Reconnect" : "Sign in with Meta"}
            </button>
          </div>
        ) : (
          <p className="text-xs text-muted">Reading Instagram needs Sign in with Meta, which an administrator sets up (META_APP_ID).</p>
        )}
        {!settings.facebook_configured && (
          <p className="text-xs text-muted">Reading Facebook Pages needs an Apify token on the server (APIFY_TOKEN).</p>
        )}
      </section>

      <section className="space-y-2" aria-label="Venues and businesses">
        <h3 className="font-medium">Venues and businesses</h3>
        <ul className="space-y-1">
          {sources.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-line p-2" data-testid="event-source">
              <span className="min-w-0 flex-1">
                {s.label ?? s.handle}{" "}
                <span className="text-xs text-muted">
                  ({s.platform === "instagram" ? `Instagram @${s.handle}` : `Facebook ${s.handle.replace(/^https:\/\/(www\.)?facebook\.com\//, "")}`})
                </span>
                {s.last_error && <span className="block text-xs text-amber-700 dark:text-amber-300">{s.last_error}</span>}
              </span>
              <label className="flex items-center gap-1 text-xs">
                <input
                  type="checkbox"
                  checked={s.enabled}
                  onChange={(ev) => void api.updateEventSource(s.id, { enabled: ev.target.checked }).then(load)}
                />
                On
              </label>
              <button className={small} aria-label={`Remove ${s.label ?? s.handle}`} onClick={() => void api.deleteEventSource(s.id).then(load)}>
                ✕
              </button>
            </li>
          ))}
        </ul>
        <form onSubmit={addSource} className="flex flex-wrap gap-2 rounded-lg border border-dashed border-line p-2">
          <select
            className={input}
            aria-label="Platform"
            value={form.platform}
            onChange={(ev) => setForm((f) => ({ ...f, platform: ev.target.value as "instagram" | "facebook" }))}
          >
            <option value="instagram">Instagram</option>
            <option value="facebook">Facebook Page</option>
          </select>
          <input
            className={`${input} min-w-40 flex-1`}
            aria-label="Account"
            placeholder={form.platform === "instagram" ? "@username" : "facebook.com/pagename"}
            value={form.handle}
            onChange={(ev) => setForm((f) => ({ ...f, handle: ev.target.value }))}
          />
          <input
            className={`${input} w-36`}
            aria-label="Name"
            placeholder="Name (optional)"
            value={form.label}
            onChange={(ev) => setForm((f) => ({ ...f, label: ev.target.value }))}
          />
          <button className={primary} disabled={!form.handle.trim()}>
            Add
          </button>
          <p className="w-full text-xs text-muted">
            Instagram accounts must be Business or Creator accounts (most venues are). Celius reads new posts, finds events
            (also on flyers, with a model that can see images) and suggests them here; nothing is added without you.
          </p>
        </form>
      </section>
    </div>
  );
}
