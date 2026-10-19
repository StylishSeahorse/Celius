import { useEffect, useState } from "react";

import { LoginPage } from "./features/auth/LoginPage";
import { Workspace } from "./features/chat/Workspace";
import { api } from "./lib/api";
import type { Me } from "./lib/types";

export function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);

  useEffect(() => {
    // Any failure (401, server down) shows the login page; signing in reports the real error.
    api
      .me()
      .then(setMe)
      .catch(() => setMe(null));
  }, []);

  if (me === undefined) {
    return <div className="flex h-full items-center justify-center text-muted">Loading…</div>;
  }
  if (me === null) return <LoginPage onLogin={setMe} />;
  return <Workspace me={me} onLogout={() => setMe(null)} />;
}
