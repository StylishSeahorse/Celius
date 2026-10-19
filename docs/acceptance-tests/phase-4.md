# Phase 4 acceptance tests: files and workspaces

Exit gate: **upload/download integrity, path security and quota tests pass.**

## Automated

| Suite | Covers |
| --- | --- |
| `tests/test_files.py` | **Integrity**: byte-identical round trips (text and binary), SHA-256 recorded; **type checks**: bytes must match the name, unknown types refused up front; **path security**: traversal, absolute, drive-letter, backslash, reserved and NUL paths refused; folder paths normalised; `resolve_within` refuses symlinks that lead out; **archives**: traversal, absolute, symlink and bomb entries rejected, limits on entries and size, office files get the same checks; **quotas**: enforced, released on delete, rejection and expiry; concurrent uploads can't overshoot; size limits and retries; dedup of identical content; privacy between accounts; auth required; ClamAV clean, infected and unavailable; jobs retried, then failed |
| `tests/test_attachments.py` | Attachment text in the prompt; not-ready, duplicate and foreign attachments refused; attachment-only messages; images only to vision models, only the latest resent; project files in project chats only; reasoning on/off/auto and per-reply override reach the template; toggle capability detected; conversation exports (MD/JSON/ZIP) and project ZIP; export validation |
| `apps/desktop` `cargo test` | Origins normalised (no file:, javascript:, credentials); paths stay inside approved roots; symlinks can't escape; listing |
| `e2e/files.spec.ts` | Attach a text file and an image → the model sees both → **the download is byte-identical (SHA-256)**; a mismatched file is refused in the UI; a folder keeps its structure; thinking off and on, kept per conversation; Markdown export and code-block download; project files reach project chats |

## Results, 2026-10-19

| Check | Result |
| --- | --- |
| API tests on SQLite | 129 passed (1 skipped: the PostgreSQL-only migration test) |
| API tests on PostgreSQL, plus migration round-trip (0001 → 0003) | 130 passed |
| Web unit tests | 15 passed |
| Playwright, desktop + Pixel 7 | 35 passed, 1 skipped (folder picking on the phone viewport) |
| Desktop Rust tests, clippy | 4 passed, no warnings |
| Desktop app on Linux (virtual display), end to end | Connect → sign in → **From PC…** → approve a folder in the native GTK picker → the listing hides a symlink to `/etc` → attach `src/main.py` and `README.md` → the model sees `celius-demo/src/main.py` → a link in the reply goes to the system opener and the app stays put |

## Manual (on the real deployment)

1. Upload a large PDF (~40 MB) from the phone over mobile data; check the extracted
   text appears and the download matches.
2. With a multimodal model (`--mmproj`), attach a photo and ask about it.
3. With a Qwen3-style model and `--jinja`, compare replies with Thinking On and Off.
4. Optional: run `clamav/clamav` next to the stack, set `CLAMAV_HOST`, and upload the
   EICAR test file; it must be refused.
