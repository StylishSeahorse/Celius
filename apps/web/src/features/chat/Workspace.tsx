import { useCallback, useEffect, useRef, useState } from "react";

import { api, isNetworkError } from "../../lib/api";
import type { ConversationSummary, Me, ModelList, Project, StorageUsage, SyncChange } from "../../lib/types";
import { cache } from "../../offline/db";
import { outbox } from "../../offline/outbox";
import { startSync } from "../../offline/sync";
import { useOnline } from "../../offline/useOnline";
import { desktop } from "../../lib/desktop";
import { DeleteConversationDialog } from "../memory/DeleteConversationDialog";
import { MemoryPanel } from "../memory/MemoryPanel";
import { ToolsPanel } from "../tools/ToolsPanel";
import { ChatView } from "./ChatView";
import { FolderBrowser } from "./FolderBrowser";
import { ProjectDialog } from "./ProjectDialog";
import { Sidebar } from "./Sidebar";
import { useChat } from "./useChat";

function idFromHash(): string | null {
  const match = window.location.hash.match(/^#\/c\/([0-9a-f-]{36})$/);
  return match ? match[1] : null;
}

function byUpdated(a: ConversationSummary, b: ConversationSummary) {
  return b.updated_at.localeCompare(a.updated_at);
}

export function Workspace({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const online = useOnline();
  const [selectedId, setSelectedId] = useState<string | null>(idFromHash);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [models, setModels] = useState<ModelList | null>(null);
  const [draftModel, setDraftModel] = useState<string | null>(null);
  const [draftReasoning, setDraftReasoning] = useState<boolean | null>(null);
  const [draftToolProfile, setDraftToolProfile] = useState<string | null>(null);
  const [draftMemory, setDraftMemory] = useState(true);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [memoryToReview, setMemoryToReview] = useState(0);
  const [deleting, setDeleting] = useState<ConversationSummary | null>(null);
  const [storage, setStorage] = useState<StorageUsage | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [queued, setQueued] = useState(outbox.size);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [pendingApprovals, setPendingApprovals] = useState(0);
  const [projectDialog, setProjectDialog] = useState<{ project: Project | null } | null>(null);
  // Desktop app only: a folder browser that resolves with the files the user picked.
  const [folderPick, setFolderPick] = useState<((files: File[]) => void) | null>(null);
  const extraAttach = desktop
    ? { label: "From PC…", pick: () => new Promise<File[]>((resolve) => setFolderPick(() => resolve)) }
    : undefined;

  const refreshList = useCallback(async () => {
    try {
      const page = await api.listConversations({ q: search || undefined, projectId });
      setConversations(page.items);
      if (!search && !projectId) void cache.putConversations(page.items, true);
    } catch (error) {
      if (!isNetworkError(error)) return;
      // Offline: filter the saved list locally.
      const saved = (await cache.conversations()).sort(byUpdated);
      const q = search.toLowerCase();
      setConversations(
        saved.filter(
          (c) =>
            (!projectId || c.project_id === projectId) &&
            (!q || (c.title ?? "").toLowerCase().includes(q)),
        ),
      );
    }
  }, [search, projectId]);

  const refreshStorage = useCallback(() => {
    api.storageUsage().then(setStorage).catch(() => undefined);
  }, []);
  useEffect(refreshStorage, [refreshStorage]);

  const refreshMemory = useCallback(() => {
    api.memorySettings().then((m) => setMemoryToReview(m.counts?.proposed ?? 0)).catch(() => undefined);
  }, []);
  useEffect(() => {
    refreshMemory();
    const timer = setInterval(refreshMemory, 30_000);
    return () => clearInterval(timer);
  }, [refreshMemory]);

  const refreshApprovals = useCallback(() => {
    api.pendingApprovals().then((items) => setPendingApprovals(items.length)).catch(() => undefined);
  }, []);
  useEffect(() => {
    refreshApprovals();
    const timer = setInterval(refreshApprovals, 15_000);
    return () => clearInterval(timer);
  }, [refreshApprovals]);

  const refreshProjects = useCallback(async () => {
    try {
      const items = await api.listProjects();
      setProjects(items);
      void cache.putProjects(items);
    } catch {
      /* keep what we have */
    }
  }, []);

  // Instant start from the device cache, then the server.
  useEffect(() => {
    void cache.conversations().then((saved) => setConversations((c) => (c.length ? c : saved.sort(byUpdated))));
    void cache.projects().then((saved) => setProjects((p) => (p.length ? p : saved)));
    void cache.get<ModelList>("models").then((saved) => setModels((m) => m ?? saved ?? null));
  }, []);

  useEffect(() => void refreshList(), [refreshList]);
  useEffect(() => void refreshProjects(), [refreshProjects]);

  useEffect(() => {
    const load = () =>
      api
        .models()
        .then((m) => {
          setModels(m);
          void cache.set("models", m);
        })
        .catch(() => undefined);
    void load();
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const onHash = () => setSelectedId(idFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const select = useCallback((id: string | null, { keepSidebar = false } = {}) => {
    window.location.hash = id ? `/c/${id}` : "";
    setSelectedId(id);
    if (!keepSidebar) setSidebarOpen(false);
  }, []);

  const chat = useChat({
    conversationId: selectedId,
    draft: {
      modelOverride: draftModel,
      projectId,
      reasoning: draftReasoning,
      toolProfile: draftToolProfile,
      memoryEnabled: draftMemory,
    },
    onConversationCreated: (id) => select(id),
    onChanged: () => {
      void refreshList();
      refreshApprovals();
    },
  });

  // Outbox: show the queue size, and deliver whenever we might be able to.
  useEffect(() => outbox.subscribe(() => setQueued(outbox.size)), []);
  useEffect(() => {
    if (online) void outbox.flush();
  }, [online]);
  useEffect(() => {
    const timer = setInterval(() => {
      if (outbox.size) void outbox.flush();
    }, 10_000);
    return () => clearInterval(timer);
  }, []);

  // Changes from other devices.
  const latest = useRef({ refreshList, refreshProjects, refreshStorage, refreshApprovals, refreshMemory, chat, selectedId, select });
  latest.current = { refreshList, refreshProjects, refreshStorage, refreshApprovals, refreshMemory, chat, selectedId, select };
  useEffect(
    () =>
      startSync((changes: SyncChange[], resync: boolean) => {
        const { refreshList, refreshProjects, refreshStorage, refreshApprovals, refreshMemory, chat, selectedId, select } =
          latest.current;
        if (changes.some((c) => c.entity_type === "memory")) refreshMemory();
        void refreshList();
        if (changes.some((c) => c.entity_type === "message")) refreshApprovals();
        if (changes.some((c) => c.entity_type === "file")) {
          refreshStorage();
          window.dispatchEvent(new Event("celius:files-changed"));
        }
        if (resync || changes.some((c) => c.entity_type === "project")) void refreshProjects();
        for (const change of changes) {
          if (change.entity_type === "conversation" && change.operation === "delete") {
            void cache.removeConversation(change.entity_id);
            if (change.entity_id === selectedId) select(null);
          }
        }
        const touchesOpen = changes.some(
          (c) =>
            c.entity_id === selectedId ||
            (c.entity_type === "message" && c.payload?.conversation_id === selectedId),
        );
        if (resync || touchesOpen) void chat.refresh();
      }),
    [],
  );

  async function remove(id: string, forgetMemories = false) {
    await api.deleteConversation(id, forgetMemories);
    void cache.removeConversation(id);
    if (id === selectedId) select(null);
    void refreshList();
  }

  async function logout() {
    await api.logout().catch(() => undefined);
    await cache.clearAll();
    onLogout();
  }

  const projectName =
    projects.find((p) => p.id === (chat.detail ? chat.detail.project_id : projectId))?.name ?? null;

  return (
    <div className="flex h-full">
      <aside
        className={`fixed inset-y-0 left-0 z-20 w-72 border-r border-line bg-panel transition-transform md:static md:translate-x-0 ${
          // Closed on mobile: off-screen *and* invisible, so it leaves the tab order and
          // the accessibility tree instead of trapping focus in an unseen panel.
          sidebarOpen ? "translate-x-0" : "-translate-x-full max-md:invisible"
        }`}
      >
        <Sidebar
          me={me}
          conversations={conversations}
          projects={projects}
          selectedId={selectedId}
          projectId={projectId}
          models={models}
          online={online}
          queued={queued}
          storage={storage}
          search={search}
          onSearch={setSearch}
          onSelect={select}
          onSelectProject={(id) => {
            // Filtering is browsing: keep the (phone) sidebar open to show the project's chats.
            setProjectId(id);
            select(null, { keepSidebar: true });
          }}
          onNewProject={() => setProjectDialog({ project: null })}
          onEditProject={(project) => setProjectDialog({ project })}
          onDelete={(id) => setDeleting(conversations.find((c) => c.id === id) ?? null)}
          onLogout={() => void logout()}
          pendingApprovals={pendingApprovals}
          onOpenTools={() => {
            setToolsOpen(true);
            setSidebarOpen(false);
          }}
          memoryToReview={memoryToReview}
          onOpenMemory={() => {
            setMemoryOpen(true);
            setSidebarOpen(false);
          }}
        />
      </aside>
      {sidebarOpen && (
        <div className="fixed inset-0 z-10 bg-black/30 md:hidden" onClick={() => setSidebarOpen(false)} />
      )}
      <ChatView
        chat={chat}
        models={models}
        online={online}
        draftModel={draftModel}
        draftReasoning={draftReasoning}
        projectName={projectName}
        onDraftModel={setDraftModel}
        onDraftReasoning={setDraftReasoning}
        draftToolProfile={draftToolProfile}
        onDraftToolProfile={setDraftToolProfile}
        draftMemory={draftMemory}
        onDraftMemory={setDraftMemory}
        onOpenSidebar={() => setSidebarOpen(true)}
        onStorageChanged={refreshStorage}
        extraAttach={extraAttach}
      />
      {folderPick && (
        <FolderBrowser
          onPick={folderPick}
          onClose={() => {
            folderPick([]);
            setFolderPick(null);
          }}
        />
      )}
      {memoryOpen && (
        <MemoryPanel
          projects={projects}
          initialTab={memoryToReview ? "review" : "remembered"}
          onChanged={refreshMemory}
          onOpenConversation={(id) => {
            setMemoryOpen(false);
            select(id);
          }}
          onClose={() => {
            setMemoryOpen(false);
            refreshMemory();
          }}
        />
      )}
      {deleting && (
        <DeleteConversationDialog
          conversation={deleting}
          onCancel={() => setDeleting(null)}
          onDelete={(forget) => {
            const id = deleting.id;
            setDeleting(null);
            void remove(id, forget).then(refreshMemory);
          }}
        />
      )}
      {toolsOpen && (
        <ToolsPanel
          isAdmin={me.is_admin}
          initialTab={pendingApprovals ? "approvals" : "activity"}
          onOpenConversation={(id) => {
            setToolsOpen(false);
            select(id);
          }}
          onPendingChanged={() => {
            refreshApprovals();
            void chat.refresh();
          }}
          onClose={() => {
            setToolsOpen(false);
            refreshApprovals();
          }}
        />
      )}
      {projectDialog && (
        <ProjectDialog
          project={projectDialog.project}
          online={online}
          onStorageChanged={refreshStorage}
          onClose={() => setProjectDialog(null)}
          onSave={async (fields) => {
            if (projectDialog.project) {
              await api.updateProject(projectDialog.project.id, fields);
            } else {
              const created = await api.createProject(fields);
              setProjectId(created.id);
              select(null, { keepSidebar: true });
            }
            await refreshProjects();
          }}
          onDelete={
            projectDialog.project
              ? async () => {
                  await api.deleteProject(projectDialog.project!.id);
                  if (projectId === projectDialog.project!.id) setProjectId(null);
                  await refreshProjects();
                  void refreshList();
                }
              : undefined
          }
        />
      )}
    </div>
  );
}
