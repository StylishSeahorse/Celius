import { useEffect, useState } from "react";

import { JoinPage } from "./features/auth/JoinPage";
import { LoginPage } from "./features/auth/LoginPage";
import { Workspace } from "./features/chat/Workspace";
import { ApiError, api, isNetworkError } from "./lib/api";
import type { Me } from "./lib/types";
import { cache } from "./offline/db";

export function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);

  useEffect(() => {
    api
      .me()
      .then((user) => {
        setMe(user);
        void cache.set("me", user);
      })
      .catch(async (error) => {
        // Offline (or server unreachable): open the saved workspace instead of the login page.
        if (isNetworkError(error) || (error instanceof ApiError && error.status >= 500)) {
          setMe((await cache.get<Me>("me")) ?? null);
        } else {
          setMe(null);
        }
      });
  }, []);

  if (me === undefined) {
    return <div className="flex h-full items-center justify-center text-muted">Loading…</div>;
  }
  if (me === null) {
    const signedIn = async (user: Me) => {
      // A different account on this device: drop the previous account's saved data.
      const previous = await cache.get<Me>("me");
      if (previous && previous.user_id !== user.user_id) await cache.clearAll();
      await cache.set("me", user);
      setMe(user);
    };
    const invitation = window.location.hash.match(/^#\/join\/([A-Za-z0-9_-]{20,100})$/)?.[1];
    if (invitation) return <JoinPage token={invitation} onJoined={signedIn} />;
    return <LoginPage onLogin={signedIn} />;
  }
  return <Workspace me={me} onLogout={() => setMe(null)} />;
}
