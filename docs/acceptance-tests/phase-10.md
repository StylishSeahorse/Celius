# Phase 10 acceptance tests: several people

Exit gate: **isolation tests between users pass for every resource; admin flows pass in
the browser tests.**

## Automated

| Suite | Covers |
| --- | --- |
| `services/api/tests/test_isolation.py` | Alice creates one of every resource: a project, a chat with a reply and a tool call awaiting approval, files, an open upload, an export, a memory, a tool profile, a schedule and a push device. Bob calls **every API route that takes an id** (generated from the OpenAPI schema) with her ids: no success, no server error, no leaked text. Listings (projects, chats, files, memories, profiles, schedules, approvals, activity, sync, push) show him nothing. Afterwards Alice's data is unchanged. In a shared project, memory recall doesn't cross people |
| `services/api/tests/test_sharing.py` | A shared project is invisible until shared. Members see its files and their chats get its instructions and files, but each person's chats stay private. Read members can't change it; write members can change content and files but not memory, archiving, deleting or sharing. Leaving keeps the member's chats, unfiled. Unknown users, the owner and bad roles are refused. Members hear about changes through sync |
| `services/api/tests/test_people.py` | An invitation creates exactly one account and can't be reused or checked again. Revoked or unknown links, taken usernames (case-insensitive), bad usernames and short passwords are refused. Only admins manage people. Admin invitations make admins. Disabling signs the person out and blocks sign-in; enabling and password reset work. Admins can't lock themselves out. Deleting removes the account's data, its unique file content and workspaces, keeps shared content and others' chats in its projects, and needs the username typed. Limits: storage quota, replies per 24 h (and clearing it), allowed models (hidden, refused, default chosen), allowed tool profiles (refused when set, narrowed when a chat runs) |
| `apps/web/e2e/people.spec.ts` (desktop and Pixel 7) | The admin creates an invitation in **Account → People**. A second browser joins with it; the link then shows as used. The admin shares a project; the member sees it marked with the owner's name and can't edit it. The admin disables the account and the member is signed out |
| SQLite test database | Foreign keys now enforced, as in PostgreSQL |

## Results, 2026-10-21 (CI run 34)

| Check | Result |
| --- | --- |
| API tests on SQLite and PostgreSQL, including the isolation sweep (370 passed, 6 skipped locally on PostgreSQL) | passed |
| Playwright, desktop + Pixel 7, including the people flow (73 passed, 1 skipped: folder picking on the phone viewport) | passed |
| Windows installers, release gate, sandbox tests | passed |

The exit gate is met. Runs 32 and 33 were red on two unreliable e2e tests (a sidebar
check made before the page had rendered after a reload; Chromium sometimes dropping a
just-shown notification from `getNotifications`). Both tests were fixed, not retried:
the people test passed 30 of 30 and the push test 200 of 200 locally.

## Manual

1. **Account → People → Invite someone**; open the link on a phone in a private window and
   create the account.
2. Share a project with them as "can use"; check they see its files but not your chats.
3. Set a 1 GB storage limit and 20 replies per day for them; check the storage bar.
4. Disable and re-enable them.
