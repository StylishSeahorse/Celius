# Gmail with "Sign in with Google"

Celius can connect Gmail with Google's own sign-in instead of an app password. Behind it
is still IMAP and SMTP, signed in with an OAuth token (XOAUTH2), so Celius reads, drafts
and sends the same way as for any other mailbox. **Sending still asks you every time.**
Design: [ADR C-14](../implementation/ARCHITECTURE_DECISIONS.md).

You set this up once per server: you create an OAuth client in Google Cloud. After that
each person signs in from Today > Settings > **Sign in with Google (Gmail)**.

## Before you start: the address Google sends people back to

Google only redirects to an address it accepts:

- It must be **https**. The one exception is `http://localhost` or `http://127.0.0.1`.
- It must be a **domain name**, not an IP address like `192.168.1.20`.
- It must end in a **public top-level domain**. Home names like `celius.lan`,
  `celius.local` or `celius.home` are refused.

The address is `APP_PUBLIC_URL` + `/api/v1/integrations/google/callback`, for example
`https://celius.example.com/api/v1/integrations/google/callback`. Signed in to Celius,
`/api/v1/integrations/google` shows it as `redirect_uri`.

If you only reach Celius on your home network by IP, you need one of these first:

- a domain with HTTPS through your reverse proxy
  ([reverse-proxy.md](reverse-proxy.md)), even if it is only reachable at home;
- Tailscale's `https://<machine>.<tailnet>.ts.net` name;
- signing in once from a browser on the server itself through `http://localhost`.
  For this, set `GOOGLE_OAUTH_REDIRECT_URL=http://localhost:8000/api/v1/integrations/google/callback`,
  and register that same address with Google.

Google only checks that the address is valid. It never connects to it, so the server
doesn't need to be reachable from the internet.

## 1. Create the OAuth client (Google Cloud Console)

1. Go to <https://console.cloud.google.com/> and create a project, e.g. "Celius".
2. **APIs & Services > Library**: enable the **Gmail API**.
3. **Google Auth Platform**, also called **OAuth consent screen**:
   - App name "Celius", your address as the support and developer contact.
   - Audience: **External**. A Google Workspace domain can choose **Internal**.
   - Data access > Add scopes: `.../auth/userinfo.email`, `openid` and
     `https://mail.google.com/` ("Read, compose, send and permanently delete all your
     email from Gmail"). Celius needs that full scope because it uses IMAP and SMTP.
   - Audience > **Test users**: add every Gmail address that will connect.
4. **Credentials > Create credentials > OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs: the address from above, exactly as it is
5. Copy the **Client ID** and **Client secret**.

## 2. Configure Celius

Add these lines to `.env`:

```
GOOGLE_OAUTH_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-...
# Only if the redirect address isn't APP_PUBLIC_URL + /api/v1/integrations/google/callback:
# GOOGLE_OAUTH_REDIRECT_URL=
```

Then restart Celius (`docker compose ... up -d`, or `make up`). The **Sign in with Google
(Gmail)** button appears in Today > Settings.

## 3. Connect

In Today > Settings, click **Sign in with Google (Gmail)**, choose the account and allow
access. You come back to Settings with "Connected you@gmail.com". To check the account,
click **Check** next to it. Doing the same again for the same address updates the
existing account rather than adding a second one.

## Keeping the connection (the 7-day limit)

While the app's publishing status is **Testing**, Google lets a sign-in last only 7 days.
After that, Check reports "Google access has expired or was revoked: sign in with Google
again".

For a lasting connection, open Google Auth Platform > Audience and click **Publish app**
(set it to "In production"). You don't need Google's verification for a personal server.
When you sign in, Google then shows "Google hasn't verified this app": click
**Advanced > Go to Celius**. An unverified app can be used by up to 100 accounts.

## Security notes

- **Credentials:** the client secret stays on the server. People's tokens are encrypted
  with `APP_SECRET_KEY`, like other credentials, and the API never returns them.
  Changing that key means signing in again.
- **Fixed servers:** a Gmail account always connects to `imap.gmail.com` and
  `smtp.gmail.com`. Its servers and password can't be edited, so the token can't be
  sent anywhere else.
- **Who the sign-in belongs to:**
  - The consent is tied to the Celius user who started it, by a sealed `state` and PKCE.
  - It is tied to that user's browser by a 10-minute cookie.
  - A sign-in started by one person can't add a mailbox to someone else's account.
- **Removing the account:** removing it in Celius revokes the access at Google. You can
  also revoke it at <https://myaccount.google.com/permissions>.
