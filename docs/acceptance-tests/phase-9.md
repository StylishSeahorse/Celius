# Phase 9 acceptance tests: phone and desktop polish

Exit gate: **notifications, previews and sharing work in the browser tests on desktop and
phone viewports.**

## Automated

| Suite | Covers |
| --- | --- |
| `services/api/tests/test_push.py` | Encryption matches the RFC 8291 example byte for byte, and round-trips with fresh keys. The VAPID token verifies against the public key; the key is stable and changes with `APP_SECRET_KEY`. Only https endpoints on push services are accepted (http, look-alike hosts and LAN addresses refused); invalid keys refused; subscribing twice doesn't duplicate; unsubscribe. A finished reply notifies every device: decrypted payload with title, body, conversation URL and tag, `aes128gcm` and `vapid` headers. A device that answers 410 is forgotten; nothing is queued for users with no device |
| `services/api/tests/test_previews.py` | A PDF gets a first-page WebP preview (512 px, not blank). A Word file shows its embedded preview, re-encoded. A spreadsheet without one has none. A broken embedded preview doesn't fail the upload. The renderer with too little memory, or a broken PDF, gives no preview rather than a crash. Previews can be switched off |
| `apps/web/e2e/notifications.spec.ts` (full Chromium, desktop and Pixel 7) | Turning notifications on registers the device with the server; turning off removes it. Pushes delivered to the real service worker show a notification, except for the chat on screen |
| `apps/web/e2e/files.spec.ts` | A PDF in a chat shows its preview card, which opens the PDF |
| `apps/web/e2e/pwa.spec.ts` | The manifest declares the share target. A multipart share (text, link, file) opens a new chat with the text in the box and the file attached; sending it works; the device's copy is deleted |
| `apps/web/e2e/desktop.spec.ts` | With the desktop bridge: a reply that finishes while the window is in the background raises a native notification with the reply text; one you're watching doesn't |
| CI `desktop` job | The Windows app builds with the tray and notification plugin (`cargo check` and the folder tests also pass on Linux) |

## Results

Filled in from the CI run on the Phase 9 head.

## Manual

1. Android, installed PWA over HTTPS: **Account → Notifications → Turn on**, then **Send a
   test**. Ask something long, switch to another app, and wait for the notification; tap it.
2. Share a web page and a photo from another app to Celius.
3. Upload a PDF and a PowerPoint saved by PowerPoint: both show previews.
4. Windows app: close the window, ask the tray icon to show it again; send a message,
   switch to another program, and wait for the Windows notification.
