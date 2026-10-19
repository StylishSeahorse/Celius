import { useCallback, useEffect, useState } from "react";

import { api } from "../../lib/api";
import type { ConversationSummary, Me, ModelList } from "../../lib/types";
import { ChatView } from "./ChatView";
import { Sidebar } from "./Sidebar";
import { useChat } from "./useChat";

function idFromHash(): string | null {
  const match = window.location.hash.match(/^#\/c\/([0-9a-f-]{36})$/);
  return match ? match[1] : null;
}

export function Workspace({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [selectedId, setSelectedId] = useState<string | null>(idFromHash);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [search, setSearch] = useState("");
  const [models, setModels] = useState<ModelList | null>(null);
  const [draftModel, setDraftModel] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const refreshList = useCallback(() => {
    api
      .listConversations(search || undefined)
      .then((page) => setConversations(page.items))
      .catch(() => undefined);
  }, [search]);

  useEffect(refreshList, [refreshList]);

  useEffect(() => {
    const load = () => api.models().then(setModels).catch(() => undefined);
    load();
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const onHash = () => setSelectedId(idFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const select = useCallback((id: string | null) => {
    window.location.hash = id ? `/c/${id}` : "";
    setSelectedId(id);
    setSidebarOpen(false);
  }, []);

  const chat = useChat({
    conversationId: selectedId,
    modelOverride: draftModel,
    onConversationCreated: select,
    onChanged: refreshList,
  });

  const modelOverride = chat.detail ? chat.detail.model_override : draftModel;

  async function changeModel(model: string | null) {
    if (!chat.detail) {
      setDraftModel(model);
      return;
    }
    const updated = await api.updateConversation(chat.detail.id, { model_override: model });
    chat.setDetail((current) =>
      current ? { ...current, model_override: updated.model_override, version: updated.version } : current,
    );
  }

  async function remove(id: string) {
    await api.deleteConversation(id);
    if (id === selectedId) select(null);
    refreshList();
  }

  async function logout() {
    await api.logout().catch(() => undefined);
    onLogout();
  }

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
          selectedId={selectedId}
          models={models}
          search={search}
          onSearch={setSearch}
          onSelect={select}
          onDelete={(id) => void remove(id)}
          onLogout={() => void logout()}
        />
      </aside>
      {sidebarOpen && (
        <div className="fixed inset-0 z-10 bg-black/30 md:hidden" onClick={() => setSidebarOpen(false)} />
      )}
      <ChatView
        chat={chat}
        models={models}
        modelOverride={modelOverride}
        onModelChange={(m) => void changeModel(m)}
        onOpenSidebar={() => setSidebarOpen(true)}
      />
    </div>
  );
}
