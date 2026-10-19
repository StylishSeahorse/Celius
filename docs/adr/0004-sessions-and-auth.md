# ADR 0004: Server-side sessions for single-user auth

Status: accepted · 2026-10-19

## Decision
Use opaque random tokens (32 bytes) and store only their SHA-256 in `sessions`.
Browsers and the PWA get an `HttpOnly; Secure; SameSite=Strict` cookie. The Tauri
client uses the same token as a bearer header. Passwords use argon2id.

## Rationale
Server-side sessions can be revoked one by one (lost phone, logout everywhere),
which stateless JWTs cannot do without a denylist. Every table carries an
owner/user id from the first migration, so multi-user support later needs no
data migration.

## Consequences
- Each authenticated request does one indexed lookup. That cost is negligible at this scale.
- CSRF: `SameSite=Strict` together with JSON-only endpoints covers the cookie path. Revisit if cross-site embedding is ever needed.
