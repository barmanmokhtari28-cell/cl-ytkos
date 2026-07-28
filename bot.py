#!/usr/bin/env python3
"""
YouTube -> Telegram forwarding bot.

Checks a fixed list of YouTube channels for new uploads, downloads each new
video (capped resolution so it fits Telegram's 50MB bot-upload limit),
translates the title to Persian, and posts it to a Telegram channel with
rich (HTML) formatting. State (which videos have already been posted) is
kept in state.json, which this script updates and the GitHub Actions
workflow commits back to the repo.
"""

import os
import re
import sys
import json
import time
import html
import datetime
import xml.etree.ElementTree as ET

import requests
import yt_dlp
from deep_translator import GoogleTranslator

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

# Add/remove channels here. "handle" is the part after the @ in the channel URL.
CHANNELS = [
    {"name": "fern",        "handle": "fern-tv"},
    {"name": "NeoExplains",  "handle": "neoexplains"},
    {"name": "Johnny Harris","handle": "johnnyharris"},
    {"name": "Vox",          "handle": "vox"},
    {"name": "ImperialYT",   "handle": "imperialyt"},
]

BOT_TOKEN = os.environ["BOT_TOKEN"]          # from GitHub Actions secret
CHANNEL_ID = os.environ["TG_CHANNEL_ID"]     # e.g. "@secretollah", from secret

STATE_FILE = "state.json"
BACKFILL_HOURS = 168        # how far back the very first run per channel looks (7 days)
MAX_TELEGRAM_MB = 49         # stay just under the 50MB bot upload limit
HEIGHT_ATTEMPTS = [480, 360, 240]   # resolution ladder to try to fit the size cap

FOOTER = "\n\n📩 @secretollah\n#یوتوب"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

COOKIES_PATH = "/tmp/yt_cookies.txt"


def prepare_cookies():
    """If a YT_COOKIES secret was provided, write it to a temp file yt-dlp
    can use. Returns the path, or None if no cookies were configured."""
    raw = os.environ.get("YT_COOKIES")
    if not raw:
        return None
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(raw)
    return COOKIES_PATH


def base_ydl_opts(cookiefile=None):
    """Options shared by every yt-dlp call: try the 'android' client first,
    since it often sidesteps YouTube's 'sign in to confirm you're not a
    bot' check without needing cookies at all; fall back to web. If a
    cookies file is available, add it too for the toughest cases."""
    opts = {
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# Channel resolution + feed parsing
# ----------------------------------------------------------------------

def resolve_channel_id(handle, cookiefile=None):
    """Resolve a @handle to a UC... channel id using yt-dlp (no download)."""
    url = f"https://www.youtube.com/@{handle}/videos"
    ydl_opts = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "playlist_items": "1",
        "skip_download": True,
        **base_ydl_opts(cookiefile),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    channel_id = info.get("channel_id") or info.get("uploader_id") or info.get("id")
    if not channel_id or not str(channel_id).startswith("UC"):
        raise RuntimeError(f"Could not resolve channel id for @{handle}: got {channel_id!r}")
    return channel_id


def fetch_recent_entries(channel_id):
    """Return list of dicts: video_id, title, published (datetime), url."""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    resp = requests.get(feed_url, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        video_id = entry.find("yt:videoId", ATOM_NS).text
        title = entry.find("atom:title", ATOM_NS).text
        published_raw = entry.find("atom:published", ATOM_NS).text
        published = datetime.datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        entries.append({
            "video_id": video_id,
            "title": title,
            "published": published,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    # oldest first, so we post in upload order
    entries.sort(key=lambda e: e["published"])
    return entries


# ----------------------------------------------------------------------
# Download
# ----------------------------------------------------------------------

def download_video(video_url, out_path_no_ext, cookiefile=None):
    """Try progressively lower resolutions until the file fits under the
    Telegram size cap. Returns (path, None) on success, or (None, reason)
    where reason is 'blocked' (yt-dlp couldn't download at all - bot-check/
    auth issue) or 'too_large' (downloaded fine but no resolution fit under
    the size cap) so the caller can report the real cause."""
    last_error = None
    got_any_file = False

    for height in HEIGHT_ATTEMPTS:
        out_tmpl = f"{out_path_no_ext}.%(ext)s"
        ydl_opts = {
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
            "merge_output_format": "mp4",
            "outtmpl": out_tmpl,
            "quiet": True,
            "noplaylist": True,
            "retries": 3,
            **base_ydl_opts(cookiefile),
        }
        # clean any leftover file from a previous attempt
        for f in os.listdir(os.path.dirname(out_path_no_ext) or "."):
            if f.startswith(os.path.basename(out_path_no_ext)):
                try:
                    os.remove(os.path.join(os.path.dirname(out_path_no_ext) or ".", f))
                except OSError:
                    pass

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
        except Exception as e:
            last_error = str(e)
            print(f"  download at <= {height}p failed: {e}", file=sys.stderr)
            continue

        final_path = f"{out_path_no_ext}.mp4"
        if not os.path.exists(final_path):
            continue

        got_any_file = True
        size_mb = os.path.getsize(final_path) / (1024 * 1024)
        print(f"  tried <= {height}p -> {size_mb:.1f} MB")
        if size_mb <= MAX_TELEGRAM_MB:
            return final_path, None
        os.remove(final_path)

    if got_any_file:
        return None, "too_large"
    if last_error and ("sign in" in last_error.lower() or "bot" in last_error.lower()):
        return None, "blocked"
    return None, "blocked" if last_error else "unknown"


# ----------------------------------------------------------------------
# Translation + caption
# ----------------------------------------------------------------------

def translate_to_persian(text):
    try:
        return GoogleTranslator(source="auto", target="fa").translate(text)
    except Exception as e:
        print(f"  translation failed: {e}", file=sys.stderr)
        return "(ترجمه در دسترس نیست)"


def build_caption(channel_name, title, translated_title):
    title_e = html.escape(title)
    title_fa = html.escape(translated_title)
    channel_e = html.escape(channel_name)
    caption = (
        f"🎬 <b>{title_e}</b>\n"
        f"📺 <i>{channel_e}</i>\n\n"
        f"🇮🇷 <b>{title_fa}</b>"
        f"{FOOTER}"
    )
    if len(caption) > 1024:
        # Telegram media captions are capped at 1024 chars - trim titles if needed
        overflow = len(caption) - 1024
        title_e = html.escape(title[: max(10, len(title) - overflow)] + "…")
        caption = (
            f"🎬 <b>{title_e}</b>\n"
            f"📺 <i>{channel_e}</i>\n\n"
            f"✨ <b>{title_fa}</b>"
            f"{FOOTER}"
        )
    return caption[:1024]


# ----------------------------------------------------------------------
# Telegram sending
# ----------------------------------------------------------------------

def send_video(file_path, caption):
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{TG_API}/sendVideo",
            data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
            files={"video": f},
            timeout=300,
        )
    ok = resp.ok and resp.json().get("ok")
    if not ok:
        print(f"  sendVideo failed: {resp.status_code} {resp.text}", file=sys.stderr)
    return ok


def send_link_fallback(video_id, caption, video_url, reason="unknown"):
    thumb = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    reason_fa = {
        "too_large": "فایل حجیم بود",
        "blocked": "دانلود مسدود شد",
        "unknown": "دانلود ناموفق بود",
    }.get(reason, "دانلود ناموفق بود")
    caption_with_link = caption + f"\n\n🔗 <a href=\"{video_url}\">تماشا در یوتیوب</a> ({reason_fa})"
    resp = requests.post(
        f"{TG_API}/sendPhoto",
        data={
            "chat_id": CHANNEL_ID,
            "photo": thumb,
            "caption": caption_with_link[:1024],
            "parse_mode": "HTML",
        },
        timeout=60,
    )
    ok = resp.ok and resp.json().get("ok")
    if not ok:
        print(f"  sendPhoto fallback failed: {resp.status_code} {resp.text}", file=sys.stderr)
    return ok


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    cookiefile = prepare_cookies()
    print("Using YouTube cookies: yes" if cookiefile else "Using YouTube cookies: no (set YT_COOKIES secret if downloads keep getting blocked)")

    state = load_state()
    now = datetime.datetime.now(datetime.timezone.utc)
    backfill_cutoff = now - datetime.timedelta(hours=BACKFILL_HOURS)

    for ch in CHANNELS:
        handle = ch["handle"]
        name = ch["name"]
        print(f"== {name} (@{handle}) ==")

        ch_state = state.setdefault(handle, {"initialized": False, "posted_ids": [], "channel_id": None})

        try:
            if not ch_state["channel_id"]:
                ch_state["channel_id"] = resolve_channel_id(handle, cookiefile)
            entries = fetch_recent_entries(ch_state["channel_id"])
        except Exception as e:
            print(f"  ERROR fetching feed: {e}", file=sys.stderr)
            continue

        first_run = not ch_state["initialized"]
        posted_ids = set(ch_state["posted_ids"])

        for entry in entries:
            if entry["video_id"] in posted_ids:
                continue

            should_post = True
            if first_run and entry["published"] < backfill_cutoff:
                should_post = False  # too old for the initial backfill window

            if should_post:
                print(f"  new video: {entry['title']} ({entry['video_id']})")
                translated = translate_to_persian(entry["title"])
                caption = build_caption(name, entry["title"], translated)

                tmp_base = f"/tmp/{entry['video_id']}"
                video_path, fail_reason = download_video(entry["url"], tmp_base, cookiefile)

                if video_path:
                    ok = send_video(video_path, caption)
                    try:
                        os.remove(video_path)
                    except OSError:
                        pass
                else:
                    print(f"  could not get the video file (reason: {fail_reason}); posting link fallback")
                    ok = send_link_fallback(entry["video_id"], caption, entry["url"], fail_reason)

                if ok:
                    time.sleep(2)  # be gentle with Telegram's API
            else:
                print(f"  skipping (older than backfill window): {entry['title']}")

            # mark as seen regardless, so it's never reprocessed
            posted_ids.add(entry["video_id"])

        ch_state["posted_ids"] = list(posted_ids)
        ch_state["initialized"] = True

    save_state(state)


if __name__ == "__main__":
    main()
