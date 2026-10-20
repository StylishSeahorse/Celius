# Phase 8 acceptance tests: reliability and scale

Exit gate: **reliability tests pass on CI (API, browser, release gate) and the dependency
audit is clean.**

## Automated

| Suite | Covers |
| --- | --- |
| `services/api/tests/test_reliability.py` | **Resumable uploads**: chunks with `Content-Range`; a chunk cut short is resumed from `GET /files/uploads/{id}`; a gap is refused with the right offset; overlapping bytes replace instead of duplicating; the stored file is byte-identical and its hash computed at completion; completing early, oversize data, a wrong total and a malformed header are refused while the upload stays resumable; another user can't see an upload's progress. **Tool use in later turns**: the next prompt replays the earlier reply with a summary of its tool calls (name, arguments, shortened result), and the summary can be switched off. **Pruning**: recent runs keep their token events; older finished runs lose only `message.delta` events; a late client still gets the terminal event and the reply |
| `apps/web/e2e/files.spec.ts` (desktop and Pixel 7) | A 9 MB file goes up in three 4 MB chunks; the second chunk's connection is reset; the upload checks where to continue and finishes; the downloaded file has the same SHA-256 |
| `.github/workflows/audit.yml` | `pip-audit` on the API and sandbox lockfiles and the sandbox image's libraries; `pnpm audit --prod` (high and critical); weekly and on dependency changes |

## Findings fixed by the first audit

| Package | Was | Now | Where |
| --- | --- | --- | --- |
| pypdf | 5.9.0 (API), 5.1.0 (sandbox) | 6.21.1 | Text extraction of every uploaded PDF: 85 advisories, mostly crafted-PDF denial of service |
| Pillow | 11.0.0 | 12.3.0 | Sandbox image |
| lxml | 5.3.0 | 6.1.0 | Sandbox image |
| requests | 2.32.3 | 2.33.0 | Sandbox image |
| Markdown | 3.7 | 3.8.1 | Sandbox image |

## Results

Filled in from the CI run on the Phase 8 head (below).
