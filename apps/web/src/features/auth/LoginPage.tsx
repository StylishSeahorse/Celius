import { type FormEvent, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { Me } from "../../lib/types";

export function LoginPage({ onLogin }: { onLogin: (me: Me) => void | Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      await onLogin(await api.me());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex h-full items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-2xl border border-line bg-panel/60 p-7 shadow-sm"
      >
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="" className="h-7 w-7" />
          <h1 className="text-xl font-semibold">Celius</h1>
        </div>
        <label className="block space-y-1">
          <span className="text-sm text-muted">Username</span>
          <input
            autoFocus
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-sm text-muted">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent"
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
        <button
          disabled={busy || !username || !password}
          className="w-full rounded-lg bg-accent py-2 font-medium text-white transition hover:bg-accent-strong disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
