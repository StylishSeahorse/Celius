# Celius Remote roadmap

What exists is described in [ARCHITECTURE.md](ARCHITECTURE.md) and its state in
[PROGRESS.md](PROGRESS.md). Next, roughly in order of value:

## Near term

1. **Real Claude Code run in CI or a lab machine.** The adapter is built on the official
   Agent SDK and tested against its message classes and a stand-in client; a nightly job
   with a test account would catch SDK behaviour changes.
2. **Windows lab verification** of the installer, the scheduled task across sign-out/sleep,
   DPAPI, `taskkill /T` with real tool chains (MSBuild, npm), and the portable `.exe`.
3. **Mid-turn instructions for Claude Code.** Today a new message waits until the turn
   ends (or the user interrupts). The SDK accepts messages while streaming; this needs a
   chat affordance for "add to the running turn" that keeps one canonical history.
4. **Attachments to remote chats.** Files attached in Celius are not copied to the
   machine (the agent is told their names). Upload through the agent's HTTPS channel into
   the session folder, with the same path checks.
5. **`attach_file` on a machine**: produced files offered as downloads in the reply
   (today: the session monitor's Files tab).
6. **Interactive terminal input** (stdin for a running command). Deliberately left out:
   it bypasses per-command approval; needs its own permission and audit design.
7. **Browser e2e for remote** (Playwright spec with the agent in `e2e-stack.sh`), once the
   repository's API job is green again (see TESTING.md).

## Platform

- **macOS agent**: the code paths are POSIX and mostly shared; needs a launchd plist,
  Keychain credential storage, and CI on `macos-latest`.
- **Signed releases and an updater**: signed artifacts (Windows code signing, checksums),
  then an opt-in updater that verifies signatures before replacing itself.
- **OS-level egress control** per machine (today recorded, not enforced, for shell
  commands): Windows Firewall rules or a local proxy when `network: restricted`.
- **Resource limits on Windows** (Job Objects for memory/CPU), matching `RLIMIT_AS` on POSIX.

## Phase 7 — distributed agents (designed for, not built)

The data model already separates machines (capabilities, load), sessions (one per
conversation) and commands, which these build on:

- **Capability-based and automatic selection**: a `selection` policy on session creation
  (`explicit` today; `auto`, `least_loaded`, `capability: {...}`, `preferred_with_fallback`)
  choosing among authorised, online machines whose capabilities match. Fallback happens
  only **before** a session starts; a started session never moves (no duplicate work).
- **Multiple agents per conversation / delegation**: a session per sub-task, linked to the
  parent conversation's agent task (ADR C-08 task state), results reported back as tool
  results.
- **Agent-to-agent messages** through Central (never machine to machine), recorded as
  session events so the conversation stays the single history.
- **Shared project context** across machines: project files and memory are already
  central; sessions opened from a project receive its instructions.
- **Scheduled remote execution**: the existing scheduled tasks gain an execution target.
- **Cross-machine workflows**: a plan whose steps carry targets, executed step by step
  with the existing checkpoint/resume machinery.
