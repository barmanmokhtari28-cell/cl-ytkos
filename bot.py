#!/usr/bin/env python3
"""
YouTube -> Telegram forwarding bot.

Checks a fixed list of YouTube channels for new uploads, translates the
title to Persian, and posts a message to a Telegram channel containing the
video link. Telegram auto-generates a native, inline-playable preview for
YouTube links (the person can watch right inside Telegram, no separate
file upload needed) - this sidesteps Telegram's 50MB bot-upload limit and
YouTube's anti-bot download blocks entirely, since nothing is downloaded.
State (which videos have already been posted) is kept in state.json,
which this script updates and the GitHub Actions workflow commits back.
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
from deep_translator import GoogleTranslator

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

# Add/remove channels here. "handle" is the part after the @ in the channel URL.
CHANNELS = [
    {"name": "fern",         "handle": "fern-tv"},
    {"name": "NeoExplains",  "handle": "neoexplains"},
    {"name": "Johnny Harris","handle": "johnnyharris"},
    {"name": "Hoog",          "handle": "hoog-youtube"},
    {"name": "IMPERIAL",   "handle": "imperialyt"},
]

BOT_TOKEN = os.environ["BOT_TOKEN"]          # from GitHub Actions secret
CHANNEL_ID = os.environ["TG_CHANNEL_ID"]     # e.g. "@secretollah", from secret

STATE_FILE = "state.json"
BACKFILL_HOURS = 168        # how far back the very first run per channel looks (7 days)

FOOTER = "\n\n📩 <b>@secretollah</b>\n#یوتوب"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


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
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# ----------------------------------------------------------------------
# Channel resolution + feed parsing
# ----------------------------------------------------------------------

CHANNEL_ID_PATTERNS = [
    re.compile(r'"channelId":"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'"externalId":"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'youtube\.com/channel/(UC[0-9A-Za-z_-]{22})'),
]


def resolve_channel_id(handle):
    """Resolve a @handle to a UC... channel id by reading the channel page's
    own HTML (no yt-dlp / no download needed for this). Retries a few times
    since YouTube occasionally serves a different page (e.g. a consent
    page) on a given request, and tries a couple of fallback patterns."""
    url = f"https://www.youtube.com/@{handle}"
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
            resp.raise_for_status()
            for pattern in CHANNEL_ID_PATTERNS:
                match = pattern.search(resp.text)
                if match:
                    return match.group(1)
            last_error = "no channel id pattern matched the page"
        except Exception as e:
            last_error = str(e)
        time.sleep(3)
    raise RuntimeError(f"Could not find channel id for @{handle} after retries: {last_error}")


def fetch_recent_entries(channel_id):
    """Return list of dicts: video_id, title, published (datetime), url."""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    resp = requests.get(feed_url, headers=HTTP_HEADERS, timeout=30)
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
# Translation + caption
# ----------------------------------------------------------------------

def translate_to_persian(text):
    try:
        return GoogleTranslator(source="auto", target="fa").translate(text)
    except Exception as e:
        print(f"  translation failed: {e}", file=sys.stderr)
        return "(ترجمه در دسترس نیست)"


def build_message(channel_name, title, translated_title, video_url):
    """Rich-text caption using Telegram's supported HTML formatting:
    bold title, italic channel name, a blockquote to set the Persian
    translation apart, and the 🔗 line hyperlinked through the channel
    name (instead of showing the raw URL as plain text)."""
    title_e = html.escape(title)
    title_fa = html.escape(translated_title)
    channel_e = html.escape(channel_name)

    def compose(t_e):
        return (
            f"🎬 <b>{t_e}</b>\n"
            f"📺 <i>{channel_e}</i>\n\n"
            f"<blockquote>🌐 {title_fa}</blockquote>\n"
            f"🔗 <a href=\"{video_url}\">{channel_e}</a>"
            f"{FOOTER}"
        )

    text = compose(title_e)
    if len(text) > 3900:  # sendMessage allows up to 4096 chars - stay comfortably under
        overflow = len(text) - 3900
        title_e = html.escape(title[: max(10, len(title) - overflow)] + "…")
        text = compose(title_e)
    return text


# ----------------------------------------------------------------------
# Telegram sending
# ----------------------------------------------------------------------

def send_video_message(text, video_url):
    """Send a text message containing the video link. Telegram will
    automatically render its native inline-playable YouTube preview
    below the text, so the video plays right in the channel."""
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({
            "url": video_url,
            "prefer_large_media": True,
            "show_above_text": False,
        }),
    }
    resp = requests.post(f"{TG_API}/sendMessage", data=payload, timeout=60)
    ok = resp.ok and resp.json().get("ok")
    if not ok:
        print(f"  sendMessage failed: {resp.status_code} {resp.text}", file=sys.stderr)
    return ok


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
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
                ch_state["channel_id"] = resolve_channel_id(handle)
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
                text = build_message(name, entry["title"], translated, entry["url"])
                ok = send_video_message(text, entry["url"])
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
