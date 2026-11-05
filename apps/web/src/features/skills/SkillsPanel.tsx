import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type SkillTrust } from "../../lib/api";
import type { Skill, SkillDetail, SkillEvent, SkillSettings } from "../../lib/types";

type Tab = "review" | "skills" | "settings";

const button = "rounded-md border border-line px-2.5 py-1 text-sm hover:bg-panel disabled:opacity-40";
const primary = "rounded-md bg-accent px-3 py-1 text-sm text-white hover:bg-accent-strong disabled:opacity-40";
const input = "w-full rounded-md border border-line bg-canvas px-2 py-1 text-sm outline-none focus:border-accent";

const TEMPLATE = `---
name: my-procedure
description: What this does and when to use it.
metadata:
  test: "true"
---

# My procedure

## When to use

## Steps
1.

## Verification
`;

const TRUST: [SkillTrust, string, string][] = [
  ["conservative", "Conservative", "Celius never creates skills on its own; you publish every skill."],
  ["balanced", "Balanced", "Celius proposes, validates and tests skills; you approve before they're used."],
  [
    "autonomous",
    "Autonomous",
    "Celius publishes skills that pass validation and their own test; untested ones still wait for you.",
  ],
];

function message(err: unknown) {
  return err instanceof ApiError ? err.message : "Something went wrong";
}

export function SkillsPanel({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [settings, setSettings] = useState<SkillSettings | null>(null);
  const [tab, setTab] = useState<Tab>("skills");
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    // Open on the review queue when something is waiting.
    api.skillSettings().then(
      (s) => {
        setSettings(s);
        if (s.pending) setTab("review");
      },
      (err) => setError(message(err)),
    );
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && (openId ? setOpenId(null) : onClose());
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, openId]);

  const changed = () => {
    setVersion((v) => v + 1);
    api.skillSettings().then(setSettings, () => undefined);
    onChanged();
  };

  return (
    <div className="fixed inset-0 z-30 flex items-stretch justify-center bg-black/40 sm:items-center sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="skills-title"
        className="flex h-full w-full max-w-3xl flex-col bg-canvas shadow-xl sm:h-[85vh] sm:rounded-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          {openId && (
            <button className={button} onClick={() => setOpenId(null)} aria-label="Back to skills">
              ←
            </button>
          )}
          <h2 id="skills-title" className="flex-1 font-semibold">
            Skills
          </h2>
          <button onClick={onClose} className={button} aria-label="Close">
            ✕
          </button>
        </header>
        {!openId && (
          <nav className="flex gap-1 border-b border-line px-3 py-2" aria-label="Skills sections">
            {(
              [
                ["review", `To review${settings?.pending ? ` (${settings.pending})` : ""}`],
                ["skills", "Skills"],
                ["settings", "Settings"],
              ] as [Tab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                aria-current={tab === key ? "page" : undefined}
                className={`rounded-md px-3 py-1 text-sm ${tab === key ? "bg-panel font-medium" : "text-muted hover:text-ink"}`}
              >
                {label}
              </button>
            ))}
          </nav>
        )}
        <div className="flex-1 space-y-3 overflow-y-auto p-4 text-sm">
          {error && <p role="alert" className="text-red-600 dark:text-red-400">{error}</p>}
          {openId ? (
            <SkillView id={openId} onChanged={changed} onDeleted={() => (setOpenId(null), changed())} />
          ) : tab === "settings" ? (
            settings && <Settings settings={settings} onSaved={setSettings} />
          ) : (
            <>
              {tab === "skills" && (
                <p className="text-xs text-muted">
                  Skills are procedures that worked before. Celius sees the few that fit your message and loads one
                  only when it uses it. A skill never changes what Celius is allowed to do.
                </p>
              )}
              {tab === "skills" && <NewSkill onCreated={(id) => (changed(), setOpenId(id))} />}
              <SkillList key={`${tab}-${version}`} pending={tab === "review"} onOpen={setOpenId} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function statusLabel(skill: Skill) {
  const pending = skill.pending_versions ?? [];
  if (pending.length) return `waiting for approval (v${pending.join(", v")})`;
  if (skill.status === "active") return `v${skill.published_version}`;
  return skill.status;
}

function SkillList({ pending, onOpen }: { pending: boolean; onOpen: (id: string) => void }) {
  const [items, setItems] = useState<Skill[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const timer = setTimeout(
      () =>
        api
          .skills(pending ? { status: "pending" } : { q: query || undefined })
          .then(setItems, (err) => setError(message(err))),
      query ? 250 : 0,
    );
    return () => clearTimeout(timer);
  }, [pending, query]);

  return (
    <div className="space-y-2">
      {!pending && (
        <input
          type="search"
          className={input}
          placeholder="Search skills"
          aria-label="Search skills"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      )}
      {error && <p role="alert" className="text-red-600 dark:text-red-400">{error}</p>}
      {items && !items.length && (
        <p className="py-6 text-center text-muted">
          {pending
            ? "Nothing to review. Skills Celius learns from finished tasks appear here first."
            : query
              ? "No matches."
              : "No skills yet. Celius proposes one after a multi-step task, or write your own."}
        </p>
      )}
      <ul className="space-y-2">
        {items?.map((s) => (
          <li key={s.id}>
            <button
              className="w-full space-y-1 rounded-lg border border-line p-3 text-left hover:bg-panel"
              onClick={() => onOpen(s.id)}
              data-testid="skill"
            >
              <span className="flex items-center gap-2">
                <span className="flex-1 font-mono text-sm">{s.name}</span>
                <span className="text-xs text-muted">{statusLabel(s)}</span>
              </span>
              <span className="block text-xs text-muted">{s.description}</span>
              {s.use_count > 0 && (
                <span className="block text-xs text-muted">
                  used {s.use_count}× · {s.success_count} worked · {s.failure_count} didn't
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function NewSkill({ onCreated }: { onCreated: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(TEMPLATE);
  const [error, setError] = useState<string | null>(null);
  if (!open)
    return (
      <button className={button} onClick={() => setOpen(true)}>
        Write a skill
      </button>
    );
  return (
    <div className="space-y-2 rounded-lg border border-dashed border-line p-3">
      <textarea
        className={`${input} font-mono`}
        rows={12}
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-label="SKILL.md"
      />
      <div className="flex gap-2">
        <button
          className={primary}
          onClick={() =>
            void api.createSkill({ "SKILL.md": text }).then(
              (s) => onCreated(s.id),
              (err) => setError(message(err)),
            )
          }
        >
          Save
        </button>
        <button className={button} onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}

function SkillView({ id, onChanged, onDeleted }: { id: string; onChanged: () => void; onDeleted: () => void }) {
  const [skill, setSkill] = useState<SkillDetail | null>(null);
  const [events, setEvents] = useState<SkillEvent[]>([]);
  const [shown, setShown] = useState<number | null>(null);
  const [file, setFile] = useState("SKILL.md");
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(() => {
    api.skill(id).then(setSkill, (err) => setError(message(err)));
    api.skillEvents(id).then(setEvents, () => setEvents([]));
  }, [id]);
  useEffect(load, [load]);
  const act = (p: Promise<unknown>, done?: string) =>
    void p.then(
      () => {
        setNote(done ?? null);
        load();
        onChanged();
      },
      (err) => setError(message(err)),
    );

  if (!skill) return error ? <p role="alert">{error}</p> : <p className="text-muted">Loading…</p>;
  const number = shown ?? skill.current?.version ?? skill.latest_version;
  const current = skill.current && skill.current.version === number ? skill.current : null;

  return (
    <div className="space-y-3">
      <div>
        <h3 className="font-mono text-base">{skill.name}</h3>
        <p className="text-muted">{skill.description}</p>
        <p className="text-xs text-muted">
          {skill.status} · {skill.origin === "generated" ? "learned from a task" : `added by ${skill.origin}`} · used{" "}
          {skill.use_count}× ({skill.success_count} worked, {skill.failure_count} didn't)
        </p>
      </div>
      {error && <p role="alert" className="text-red-600 dark:text-red-400">{error}</p>}
      {note && <p role="status" className="text-xs">{note}</p>}

      <section className="space-y-1">
        <h4 className="font-medium">Versions</h4>
        <ul className="space-y-1">
          {skill.versions.map((v) => (
            <li key={v.version} className="flex flex-wrap items-center gap-2 rounded-md border border-line px-2 py-1">
              <button className="underline" onClick={() => (setShown(v.version), setFile("SKILL.md"))}>
                v{v.version}
              </button>
              <span className="text-xs text-muted">
                {v.status} · test {String(v.test?.status ?? "–")} · by {v.created_by}
                {v.changelog ? ` · ${v.changelog}` : ""}
              </span>
              {!v.validation?.ok && Array.isArray(v.validation?.errors) && (
                <span className="text-xs text-red-600 dark:text-red-400">
                  {(v.validation?.errors as string[] | undefined)?.join("; ")}
                </span>
              )}
              <span className="flex-1" />
              {v.status !== "published" && Boolean(v.validation?.ok) && (
                <button className={primary} onClick={() => act(api.publishSkillVersion(skill.id, v.version), `Published v${v.version}`)}>
                  {v.status === "retired" ? "Roll back to this" : "Approve & publish"}
                </button>
              )}
              {(v.status === "pending" || v.status === "candidate" || v.status === "validated") && (
                <button className={button} onClick={() => act(api.rejectSkillVersion(skill.id, v.version))}>
                  Reject
                </button>
              )}
              {Boolean(v.validation?.ok) && v.test?.status !== "not_defined" && (
                <button
                  className={button}
                  onClick={() => act(api.testSkillVersion(skill.id, v.version), "Test queued; the result appears here")}
                >
                  Test
                </button>
              )}
            </li>
          ))}
        </ul>
      </section>

      <VersionFiles skillId={skill.id} number={number} current={current} file={file} onFile={setFile} />

      {editing !== null ? (
        <div className="space-y-2">
          <textarea
            className={`${input} font-mono`}
            rows={14}
            value={editing}
            onChange={(e) => setEditing(e.target.value)}
            aria-label="Edit SKILL.md"
          />
          <div className="flex gap-2">
            <button
              className={primary}
              onClick={() =>
                act(
                  api
                    .addSkillVersion(skill.id, { ...(current?.files ?? {}), "SKILL.md": editing }, "Edited by you")
                    .then(() => setEditing(null)),
                  "Saved as a new version",
                )
              }
            >
              Save as new version
            </button>
            <button className={button} onClick={() => setEditing(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {current && (
            <button className={button} onClick={() => setEditing(current.files["SKILL.md"] ?? "")}>
              Edit
            </button>
          )}
          <a className={button} href={api.skillExportUrl(skill.id)}>
            Export
          </a>
          <button className={button} onClick={() => act(api.skillFeedback(skill.id, "success"), "Thanks, noted")}>
            It worked
          </button>
          <button className={button} onClick={() => act(api.skillFeedback(skill.id, "failure"), "Noted")}>
            It didn't work
          </button>
          {skill.status === "deprecated" ? (
            <button className={button} onClick={() => act(api.restoreSkill(skill.id))}>
              Restore
            </button>
          ) : (
            <button className={button} onClick={() => act(api.deprecateSkill(skill.id))}>
              Deprecate
            </button>
          )}
          <button
            className={button}
            onClick={() =>
              confirm(`Delete ${skill.name} and all its versions?`) &&
              void api.deleteSkill(skill.id).then(onDeleted, (err) => setError(message(err)))
            }
          >
            Delete
          </button>
        </div>
      )}

      {events.length > 0 && (
        <section className="space-y-1">
          <h4 className="font-medium">History</h4>
          <ul className="space-y-0.5 text-xs text-muted">
            {events.slice(0, 30).map((e) => (
              <li key={e.id}>
                {new Date(e.created_at).toLocaleString()} · {e.version ? `v${e.version} · ` : ""}
                {e.kind} {e.outcome}
                {typeof e.detail?.action === "string" ? ` (${e.detail.action})` : ""}
                {e.duration_ms ? ` · ${Math.round(e.duration_ms / 100) / 10}s` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function VersionFiles({
  skillId,
  number,
  current,
  file,
  onFile,
}: {
  skillId: string;
  number: number;
  current: SkillDetail["current"];
  file: string;
  onFile: (f: string) => void;
}) {
  const [loaded, setLoaded] = useState<SkillDetail["current"]>(current);
  useEffect(() => {
    if (current) setLoaded(current);
    else
      api.skillVersion(skillId, number).then(setLoaded, () => setLoaded(null));
  }, [skillId, number, current]);
  if (!loaded) return null;
  const files = Object.keys(loaded.files).sort((a, b) => (a === "SKILL.md" ? -1 : b === "SKILL.md" ? 1 : a.localeCompare(b)));
  const warnings = (loaded.validation?.warnings as string[] | undefined) ?? [];
  return (
    <section className="space-y-1">
      <div className="flex flex-wrap gap-1">
        {files.map((f) => (
          <button
            key={f}
            onClick={() => onFile(f)}
            className={`rounded px-2 py-0.5 font-mono text-xs ${f === file ? "bg-panel font-medium" : "text-muted"}`}
          >
            {f}
          </button>
        ))}
      </div>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-panel p-3 font-mono text-xs" data-testid="skill-file">
        {loaded.files[file] ?? ""}
      </pre>
      {warnings.length > 0 && <p className="text-xs text-amber-700 dark:text-amber-300">Warnings: {warnings.join("; ")}</p>}
      {typeof loaded.test?.output === "string" && loaded.test.output && (
        <details className="text-xs">
          <summary>Last test output ({String(loaded.test.status)})</summary>
          <pre className="whitespace-pre-wrap">{loaded.test.output}</pre>
        </details>
      )}
    </section>
  );
}

function Settings({ settings, onSaved }: { settings: SkillSettings; onSaved: (s: SkillSettings) => void }) {
  const [error, setError] = useState<string | null>(null);
  const save = (body: { trust?: SkillTrust; auto_generate?: boolean }) => {
    onSaved({ ...settings, ...body }); // show the choice at once; the server's answer follows
    void api.updateSkillSettings(body).then(onSaved, (err) => (setError(message(err)), onSaved(settings)));
  };
  return (
    <div className="space-y-4">
      <fieldset className="space-y-2">
        <legend className="font-medium">How much Celius may do on its own</legend>
        {TRUST.map(([key, label, help]) => (
          <label key={key} className="flex items-start gap-2">
            <input type="radio" name="trust" className="mt-1" checked={settings.trust === key} onChange={() => save({ trust: key })} />
            <span>
              {label}
              <span className="block text-xs text-muted">{help}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          className="mt-1"
          checked={settings.auto_generate}
          disabled={settings.trust === "conservative"}
          onChange={(e) => save({ auto_generate: e.target.checked })}
        />
        <span>
          Learn skills from finished tasks
          <span className="block text-xs text-muted">
            After a task that took several tool steps, the model checks whether it's worth saving (uses some model
            time).
          </span>
        </span>
      </label>
      <p className="text-xs text-muted">
        Skills only describe steps. Whatever a skill says, every tool call still follows your tool permissions and
        approvals, and code runs only in the sandbox.
      </p>
      {error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}
