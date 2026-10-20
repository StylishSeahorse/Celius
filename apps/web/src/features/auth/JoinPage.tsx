import { type FormEvent, useEffect, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { Me } from "../../lib/types";

const input =
  "w-full rounded-lg border border-line bg-canvas px-3 py-2 outline-none focus:border-accent";

/** Create an account from an invitation link (#/join/<token>). */
export function JoinPage({ token, onJoined }: { token: string; onJoined: (me: Me) => void | Promise<void> }) {
  const [valid, setValid] = useState<boolean | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .checkInvitation(token)
      .then((invitation) => {
        setValid(true);
        setNote(invitation.note);
      })
      .catch(() => setValid(false));
  }, [token]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password !== again) return setError("The passwords don't match");
    setBusy(true);
    try {
      await api.register({ token, username, password, display_name: displayName || null });
      window.location.hash = "";
      await onJoined(await api.me());
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 422
          ? "Usernames are 2–32 letters, digits, dots, dashes or underscores; passwords at least 12 characters"
          : err instanceof ApiError
            ? err.message
            : "Could not reach the server",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex h-full items-center justify-center px-4">
      <form
        onSubmit={submit}
        aria-label="Create your account"
        className="w-full max-w-sm space-y-4 rounded-2xl border border-line bg-panel/60 p-7 shadow-sm"
      >
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="" className="h-7 w-7" />
          <h1 className="text-xl font-semibold">Join Celius</h1>
        </div>
        {valid === null && <p className="text-sm text-muted">Checking the invitation…</p>}
        {valid === false && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            This invitation link has expired or was already used. Ask for a new one.
          </p>
        )}
        {valid && (
          <>
            {note && <p className="text-sm text-muted">{note}</p>}
            <label className="block space-y-1">
              <span className="text-sm text-muted">Username</span>
              <input
                autoFocus
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className={input}
              />
            </label>
            <label className="block space-y-1">
              <span className="text-sm text-muted">Your name (optional)</span>
              <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className={input} />
            </label>
            <label className="block space-y-1">
              <span className="text-sm text-muted">Password (12+ characters)</span>
              <input
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={input}
              />
            </label>
            <label className="block space-y-1">
              <span className="text-sm text-muted">Password again</span>
              <input
                type="password"
                autoComplete="new-password"
                value={again}
                onChange={(e) => setAgain(e.target.value)}
                className={input}
              />
            </label>
            {error && (
              <p role="alert" className="text-sm text-red-600 dark:text-red-400">
                {error}
              </p>
            )}
            <button
              disabled={busy || !username || password.length < 12}
              className="w-full rounded-lg bg-accent px-3 py-2 font-medium text-white disabled:opacity-50"
            >
              Create account
            </button>
          </>
        )}
      </form>
    </main>
  );
}
