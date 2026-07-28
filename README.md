# YouTube → Telegram forwarding bot

Checks fern-tv, NeoExplains, Johnny Harris, Vox, and ImperialYT for new
uploads every 30 minutes, downloads new videos, translates the title to
Persian, and posts to your Telegram channel with the actual video file
(not just a thumbnail). Runs entirely on GitHub Actions — free, 24/7, no
device or server of yours involved.

## Before you do anything: rotate your bot token

You pasted your bot token in a chat, so treat it as already exposed.
Open Telegram, message **@BotFather** → `/mybots` → select your bot →
**API Token** → **Revoke current token**, and use the *new* token below.
Never put it directly in code — only in a GitHub secret (step 3).

## 1. Create the repo

- Go to github.com → **New repository** → make it **Public** (this is
  fine — your secrets, below, are never stored in the repo itself).
- Upload all the files in this project keeping the folder structure,
  including `.github/workflows/check.yml` (GitHub's web "Add file →
  Upload files" supports dragging in a folder and preserves the path).

## 2. Add your bot to the channel

- Add your bot (`@your_bot_username`) to **@secretollah** as an
  **admin** with permission to post messages. It can't post without this.

## 3. Add your secrets

In the repo: **Settings → Secrets and variables → Actions → New
repository secret**, add two:

| Name | Value |
|---|---|
| `BOT_TOKEN` | your new bot token from BotFather |
| `TG_CHANNEL_ID` | `@secretollah` |

These never appear in the repo's code or history — only inside Actions'
encrypted secret store, and they're masked in logs.

## 4. Run it

- Go to the **Actions** tab → "Check for new videos" → **Run workflow**
  (this triggers the first run manually so you don't have to wait for
  the schedule). On this first run per channel, it backfills only
  videos from the last 2 days, then goes fully incremental after that.
- After that it runs automatically every 30 minutes via the schedule in
  `check.yml` — no further action needed.

## Limits to know about

- **Telegram bots can only upload files up to 50MB.** The script tries
  480p, then 360p, then 240p to fit under that. If a video still doesn't
  fit at any of those, it posts the thumbnail + title + a link to watch
  on YouTube instead of the file, so you never lose the notification
  entirely — you just don't get the video file for that one.
- **YouTube sometimes blocks cloud IPs** (including GitHub's) with a
  "Sign in to confirm you're not a bot" error. If you start seeing
  `download failed` errors in the Actions logs, the fix is to export
  YouTube cookies from your own logged-in browser (via a browser
  extension like "Get cookies.txt"), add the file's contents as a new
  secret `YT_COOKIES`, and I can adjust the script to use it — just come
  back and tell me if this happens.
- Public repos get free GitHub Actions minutes for scheduled jobs like
  this; nothing to pay as long as you stay on the public tier.

## Adjusting things later

- Add/remove channels: edit the `CHANNELS` list at the top of `bot.py`.
- Change how often it checks: edit the cron line in `check.yml`.
- Change the caption footer or format: edit `build_caption()` in `bot.py`.
