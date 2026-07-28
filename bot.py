#!/usr/bin/env python3
"""
YouTube -> Telegram forwarding bot.

Checks a fixed list of YouTube channels for new uploads, translates the title
to Persian, and posts a message to a Telegram channel with a high-resolution 
playable video preview embed using Telegram's Link Preview engine. State (which 
videos have already been posted) is kept in state.json, which this script updates 
and the GitHub Actions workflow commits back to the repo.
"""

import os
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

FOOTER = "\n\n📩 @secretollah\n#یوتوب"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
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
    """Options used by yt-dlp to resolve channel handles."""
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
    """Resolve a @handle to a UC... channel id using yt-dlp."""
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
# Translation + message building
# ----------------------------------------------------------------------

def translate_to_persian(text):
    try:
        return GoogleTranslator(source="auto", target="fa").translate(text)
    except Exception as e:
        print(f"  translation failed: {e}", file=sys.stderr)
        return "(ترجمه در دسترس نیست)"


def build_message(channel_name, title, translated_title, video_url):
    title_e = html.escape(title)
    title_fa = html.escape(translated_title)
    channel_e = html.escape(channel_name)
    
    message = (
        f"🎬 <b>{title_e}</b>\n"
        f"📺 <i>{channel_e}</i>\n\n"
        f"🌐 <b>{title_fa}</b>\n\n"
        f"🔗 {video_url}"
        f"{FOOTER}"
    )
    return message


# ----------------------------------------------------------------------
# Telegram sending
# ----------------------------------------------------------------------

def send_telegram_post(message_text, video_url):
    """Posts text with YouTube large video link preview enabled."""
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message_text,
        "parse_mode": "HTML",
        "link_preview_options": {
            "url": video_url,
            "prefer_large_media": True,
            "show_above_text": False,
        }
    }
    
    resp = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)
    ok = resp.ok and resp.json().get("ok", False)
    if not ok:
        print(f"  sendMessage failed: {resp.status_code} {resp.text}", file=sys.stderr)
    return ok


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    cookiefile = prepare_cookies()
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
                msg_text = build_message(name, entry["title"], translated, entry["url"])

                ok = send_telegram_post(msg_text, entry["url"])
                if ok:
                    time.sleep(2)  # be gentle with Telegram's API
                else:
                    print(f"  failed to post message for video {entry['video_id']}", file=sys.stderr)
            else:
                print(f"  skipping (older than backfill window): {entry['title']}")

            # mark as seen regardless, so it's never reprocessed
            posted_ids.add(entry["video_id"])

        ch_state["posted_ids"] = list(posted_ids)
        ch_state["initialized"] = True

    save_state(state)


if __name__ == "__main__":
    main()
