# Event finder: events from venues' Instagram and Facebook posts

Celius reads the recent posts of the venues and businesses you list, finds the events in
them (also on flyers, with a model that can see images), and suggests them in Today >
Events. Each one you approve goes into your calendar, for example Nextcloud. Nothing is
added without you. Design: [ADR C-17](../implementation/ARCHITECTURE_DECISIONS.md).

| Source | How Celius reads it | What you set up |
|---|---|---|
| Instagram Business or Creator accounts | Meta's official Business Discovery API | A Meta app, then "Sign in with Meta" |
| Facebook Pages | A hosted scraper (Apify), because Meta has no open API for other Pages' posts | An Apify token |

Celius never sees your Facebook or Instagram password: you sign in on Meta's own page.

> **Checked against the docs?** No. The Graph API calls and the Apify input follow those
> APIs as I know them, but their current documentation couldn't be read while this was
> built. If something is refused, the reason shows on the source in Today > Events. The
> Graph API version is a setting (`META_GRAPH_VERSION`), and all Meta-specific code is in
> `services/api/app/services/integrations/meta.py`.

## 1. Your Instagram account

Business Discovery reads *other* accounts through *your* Instagram account. Your
account must be:
- a **Professional** account (Business or Creator). Switch in the Instagram app under
  Settings > Account type and tools; it's free and reversible;
- **linked to a Facebook Page** you manage. Any Page will do, even an empty one made for
  this.

The accounts you watch must be Business or Creator accounts too. Most venues are;
personal accounts can't be read.

## 2. The Meta app

1. Go to <https://developers.facebook.com/apps> and create an app (type **Business**, or
   the use case for Instagram and Facebook Login, depending on the current dashboard).
2. Add **Facebook Login** (Facebook Login for Business). Under its settings, add this to
   **Valid OAuth Redirect URIs**:
   `https://<your Celius domain>/api/v1/integrations/meta/callback`
   As with Google, it must be https on a real domain.
3. Permissions Celius asks for: `instagram_basic`, `pages_show_list`,
   `pages_read_engagement`, `business_management`.
4. Leave the app in **Development mode**. As its admin, you can use these permissions on
   your own accounts without Meta's App Review.
5. Copy the **App ID** and **App secret** (Settings > Basic).

## 3. Apify (for Facebook Pages; optional)

1. Create an account at <https://apify.com> and copy your API token (Settings >
   Integrations).
2. Celius uses the **Facebook Posts Scraper** actor (`apify~facebook-posts-scraper`).
   Runs cost a little; Apify's free monthly credit usually covers a few Pages every few
   days.

## 4. Configure Celius

In `.env`:

```
META_APP_ID=1234567890
META_APP_SECRET=...
# Only if the redirect URI isn't APP_PUBLIC_URL + /api/v1/integrations/meta/callback:
# META_REDIRECT_URL=
# META_GRAPH_VERSION=v23.0
APIFY_TOKEN=apify_api_...
# EVENT_SCAN_DAYS=4             default for new users; each user can change it
# EVENT_POSTS_PER_SOURCE=12
# EVENT_MAX_POSTS_PER_SCAN=60
```

Then restart Celius with `make up`.

## 5. Use it

1. **Calendar:** connect your Nextcloud calendar in Today > Settings (CalDAV), if it
   isn't already.
2. **Sign in:** in Today > **Events**, click **Sign in with Meta** and allow access. You
   come back to Events with "Connected Instagram @you".
3. **Choose the calendar:** under **Add to**, pick the Nextcloud calendar. Approving an
   event also remembers the calendar it went to.
4. **Add sources:** add venues by Instagram username (`@thefoxbar`) or Facebook Page
   address (`facebook.com/thefoxbar`). A name is optional, and is used as the venue when
   a post doesn't say.
5. **Scan:** click **Scan now** for the first scan, which reads the last 3 weeks. After
   that, Celius checks every 4 days by default (Check every…) and only reads new posts.
6. **Review:** found events show with the date, time, venue, price, and links to the post
   and tickets. **Add to calendar** puts one in the calendar you chose, with the links
   in its description; **Dismiss** drops it. A phone notification says when new events
   are found.

**Flyers:** the details are often only in the image. Celius sends the images to the
model only if it can see images (Account > Models shows "images"). With a text-only model,
events that only appear in the image are missed.

## Notes

- **Duplicates:** the same event posted by the venue and the band becomes one suggestion
  ("also posted by 1 more").
- **Expiry and limits:**
  - The Page token Celius keeps doesn't expire while you keep the app.
  - If Meta withdraws access (password change, app removed), the source shows "sign in
    with Meta again"; click **Reconnect**.
  - Meta's rate limits are generous for a few dozen accounts every few days. When one is
    reached, the next scan tries again.
- **Removing access:** delete the Instagram account under Today > Settings, and remove the
  app in Facebook under Settings > Business Integrations.
