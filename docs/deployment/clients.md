# Installing the clients

All clients talk to the same server and account. History, projects and settings are
shared and stay in sync.

## Browser
Open your server's address (for example `http://192.168.0.204:8000` or `https://ai.example.com`).

## Android (installable web app)
1. Open the server's **HTTPS** address in Chrome.
2. Menu ⋮ → **Install app** (or **Add to Home screen**).

The installed app opens full-screen. It shows saved conversations when you're offline,
and messages you write offline are sent automatically when you reconnect.

> **HTTPS is required for installing and for opening offline.** Browsers only allow
> service workers on secure origins. Over plain `http://192.168.x.x` the site still works
> and still queues messages written offline while it's open, but it can't be installed
> or opened from cold while offline. Put it behind your reverse proxy with a certificate
> (see [reverse-proxy.md](reverse-proxy.md)).

## Windows desktop app
1. Download the installer from the latest CI run: GitHub → Actions → **ci** → the
   `celius-windows-installers` artifact (`Celius_x.y.z_x64-setup.exe` or `.msi`).
2. Run it. It installs for the current user; WebView2 is installed if missing.
3. On first start, enter your server address and choose **Connect**.

Change the server later from the **Celius → Change server…** menu.

**Attaching files from your PC:** in the message box choose **From PC…**, then
**Approve a folder…** and pick it in the Windows folder dialog. Celius can read only
folders you approved there; it uploads only the files you tick. Folders are never
shared automatically, and **Stop sharing this folder** removes the approval. Links in
replies open in your normal browser.

The installers aren't code-signed yet, so Windows SmartScreen will warn
("Windows protected your PC" → **More info** → **Run anyway**). Signing needs an
Authenticode certificate (backlog S-08).

To build it yourself on Windows: install Node 22, pnpm, Rust (rustup) and the
[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), then run
`pnpm install` and `pnpm --filter @celius/desktop build`.
