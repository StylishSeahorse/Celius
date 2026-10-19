import { useEffect, useState } from "react";

import type { ConversationSummary, Me, ModelList, Project } from "../../lib/types";

interface Props {
  me: Me;
  conversations: ConversationSummary[];
  projects: Project[];
  selectedId: string | null;
  projectId: string | null;
  models: ModelList | null;
  online: boolean;
  queued: number;
  search: string;
  onSearch: (q: string) => void;
  onSelect: (id: string | null) => void;
  onSelectProject: (id: string | null) => void;
  onNewProject: () => void;
  onEditProject: (project: Project) => void;
  onDelete: (id: string) => void;
  onLogout: () => void;
}

export function Sidebar(props: Props) {
  const { conversations, projects, selectedId, projectId, models, onSelect, onSearch } = props;
  const [query, setQuery] = useState(props.search);

  useEffect(() => {
    const timer = setTimeout(() => onSearch(query), 250);
    return () => clearTimeout(timer);
  }, [query, onSearch]);

  const activeProject = projects.find((p) => p.id === projectId) ?? null;

  return (
    <nav className="flex h-full flex-col gap-3 p-3" aria-label="Conversations">
      <div className="flex items-center gap-2 px-1 pt-1">
        <img src="/favicon.svg" alt="" className="h-6 w-6" />
        <span className="font-semibold">Celius</span>
      </div>
      <button
        onClick={() => onSelect(null)}
        className="rounded-lg border border-line bg-canvas px-3 py-2 text-left text-sm font-medium hover:border-accent/60"
      >
        + New chat{activeProject ? ` in ${activeProject.name}` : ""}
      </button>
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search"
        aria-label="Search conversations"
        className="rounded-lg border border-line bg-canvas px-3 py-1.5 text-sm outline-none focus:border-accent"
      />

      <section aria-label="Projects" className="space-y-0.5">
        <div className="flex items-center justify-between px-1">
          <h2 className="text-xs font-medium tracking-wide text-muted uppercase">Projects</h2>
          <button
            onClick={props.onNewProject}
            disabled={!props.online}
            className="rounded px-1.5 text-sm text-muted hover:text-ink disabled:opacity-40"
            aria-label="New project"
          >
            +
          </button>
        </div>
        <ul className="-mx-1 max-h-40 overflow-y-auto">
          <li>
            <button
              onClick={() => props.onSelectProject(null)}
              aria-current={projectId === null ? "true" : undefined}
              className={`w-full rounded-lg px-3 py-1.5 text-left text-sm ${projectId === null ? "bg-canvas font-medium" : "hover:bg-canvas/60"}`}
            >
              All chats
            </button>
          </li>
          {projects.map((p) => (
            <li key={p.id} className="group relative">
              <button
                onClick={() => props.onSelectProject(p.id)}
                aria-current={p.id === projectId ? "true" : undefined}
                className={`w-full truncate rounded-lg px-3 py-1.5 pr-14 text-left text-sm ${p.id === projectId ? "bg-canvas font-medium" : "hover:bg-canvas/60"}`}
              >
                {p.name}
              </button>
              <button
                onClick={() => props.onEditProject(p)}
                aria-label={`Project settings for ${p.name}`}
                className="absolute top-1/2 right-1.5 -translate-y-1/2 rounded px-1.5 text-xs text-muted hover:text-ink md:hidden md:group-hover:block"
              >
                ⋯
              </button>
            </li>
          ))}
        </ul>
      </section>

      <ul className="-mx-1 flex-1 space-y-0.5 overflow-y-auto border-t border-line pt-2" aria-label="Chats">
        {conversations.map((c) => (
          <li key={c.id} className="group relative">
            <button
              onClick={() => onSelect(c.id)}
              aria-current={c.id === selectedId ? "page" : undefined}
              className={`w-full truncate rounded-lg px-3 py-2 pr-8 text-left text-sm ${
                c.id === selectedId ? "bg-canvas font-medium" : "hover:bg-canvas/60"
              }`}
            >
              {c.title || "Untitled"}
            </button>
            <button
              onClick={() => {
                if (confirm(`Delete “${c.title || "Untitled"}”?`)) props.onDelete(c.id);
              }}
              disabled={!props.online}
              aria-label={`Delete ${c.title || "conversation"}`}
              className="absolute top-1/2 right-1.5 hidden -translate-y-1/2 rounded px-1.5 text-muted group-hover:block hover:text-ink"
            >
              ×
            </button>
          </li>
        ))}
        {conversations.length === 0 && (
          <li className="px-3 py-2 text-sm text-muted">
            {props.search ? "No matches" : "No conversations yet"}
          </li>
        )}
      </ul>

      <div className="space-y-2 border-t border-line pt-3 text-sm">
        <div className="flex items-center gap-2 px-1 text-muted" title={models?.health.detail ?? undefined}>
          <span
            className={`h-2 w-2 rounded-full ${
              !props.online ? "bg-amber-500" : models === null ? "bg-muted" : models.health.ok ? "bg-emerald-500" : "bg-red-500"
            }`}
          />
          {!props.online
            ? `Offline${props.queued ? ` · ${props.queued} queued` : ""}`
            : models === null
              ? "Checking model…"
              : models.health.ok
                ? "Model server online"
                : "Model server offline"}
        </div>
        <div className="flex items-center justify-between px-1">
          <span className="truncate">{props.me.display_name ?? props.me.username}</span>
          <button onClick={props.onLogout} className="text-muted hover:text-ink">
            Sign out
          </button>
        </div>
      </div>
    </nav>
  );
}
