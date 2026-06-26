#!/usr/bin/env python3
"""
telegram_rss.py — Build RSS feeds from public Telegram channels.

Reads channel usernames from channels.txt, fetches each channel's public
web preview (https://t.me/s/<channel>), parses recent posts, and writes
one RSS file per channel plus a combined feed into docs/feeds/.
"""

from __future__ import annotations

import html
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

# ---- Configuration ---------------------------------------------------------

CHANNELS_FILE = Path("channels.txt")
OUTPUT_DIR = Path("docs/feeds")
SITE_TITLE = "Telegram → RSS"
MAX_POSTS = 20          # posts kept per channel feed
COMBINED_LIMIT = 100    # posts kept in the combined feed
REQUEST_TIMEOUT = 30    # seconds
RETRIES = 3
RETRY_DELAY = 5         # seconds between retries
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Base URL for GitHub Pages links (self-links inside feeds, index page).
# Set automatically by the workflow via the PAGES_BASE_URL env var, e.g.
#   https://username.github.io/telegram-rss
PAGES_BASE_URL = os.environ.get("PAGES_BASE_URL", "").rstrip("/")

# ---- Channel list parsing --------------------------------------------------

def normalize_channel(raw: str) -> str | None:
    """Turn '@name', 'name', or a t.me URL into a bare channel username."""
    s = raw.strip()
    if not s or s.startswith("#"):
        return None
    s = s.split("#", 1)[0].strip()           # allow inline comments
    s = re.sub(r"^https?://t\.me/", "", s)    # strip URL prefix
    s = re.sub(r"^s/", "", s)                 # strip /s/
    s = s.lstrip("@").strip("/")
    s = s.split("/")[0]                        # drop any /123 post id
    return s or None


def read_channels(path: Path) -> list[str]:
    if not path.exists():
        print(f"ERROR: {path} not found.", file=sys.stderr)
        return []
    seen: set[str] = set()
    channels: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = normalize_channel(line)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            channels.append(name)
    return channels

# ---- Fetching --------------------------------------------------------------

def fetch_channel_html(channel: str) -> str | None:
    url = f"https://t.me/s/{channel}"
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            print(f"  {channel}: HTTP {resp.status_code} (attempt {attempt})",
                  file=sys.stderr)
        except requests.RequestException as exc:
            print(f"  {channel}: request error {exc} (attempt {attempt})",
                  file=sys.stderr)
        if attempt < RETRIES:
            time.sleep(RETRY_DELAY)
    return None

# ---- Parsing ---------------------------------------------------------------

def extract_bg_url(style: str | None) -> str | None:
    if not style:
        return None
    m = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", style)
    return m.group(1) if m else None


def parse_posts(channel: str, page_html: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(page_html, "lxml")

    title_tag = soup.select_one(".tgme_channel_info_header_title")
    channel_title = title_tag.get_text(strip=True) if title_tag else f"@{channel}"

    posts: list[dict] = []
    for msg in soup.select(".tgme_widget_message"):
        data_post = msg.get("data-post")          # "channel/1234"
        date_anchor = msg.select_one("a.tgme_widget_message_date")
        time_tag = date_anchor.select_one("time[datetime]") if date_anchor else None

        # Permalink
        if data_post:
            link = f"https://t.me/{data_post}"
        elif date_anchor and date_anchor.get("href"):
            link = date_anchor["href"]
        else:
            continue  # cannot identify the post

        # Publication date
        published = None
        if time_tag and time_tag.get("datetime"):
            try:
                published = datetime.fromisoformat(time_tag["datetime"])
            except ValueError:
                published = None
        if published is None:
            published = datetime.now(timezone.utc)
        elif published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        # Text — keep inner HTML for rich content, derive plain text separately
        text_tag = msg.select_one(".tgme_widget_message_text")
        text_html, text_plain = "", ""
        if text_tag:
            text_html = text_tag.decode_contents().strip()
            tmp = BeautifulSoup(str(text_tag), "lxml")
            for br in tmp.find_all("br"):
                br.replace_with("\n")
            text_plain = re.sub(r"\n{3,}", "\n\n", tmp.get_text("\n")).strip()

        # Media (photos / video thumbnails)
        media_urls: list[str] = []
        for ph in msg.select("a.tgme_widget_message_photo_wrap"):
            u = extract_bg_url(ph.get("style"))
            if u:
                media_urls.append(u)
        for vt in msg.select("i.tgme_widget_message_video_thumb"):
            u = extract_bg_url(vt.get("style"))
            if u:
                media_urls.append(u)

        has_video = bool(
            msg.select_one(".tgme_widget_message_video")
            or msg.select_one("i.tgme_widget_message_video_thumb")
        )

        posts.append({
            "link": link,
            "published": published,
            "text_plain": text_plain,
            "text_html": text_html,
            "media": media_urls,
            "has_video": has_video,
        })

    posts.sort(key=lambda p: p["published"], reverse=True)
    return channel_title, posts[:MAX_POSTS]

# ---- Feed building ---------------------------------------------------------

def build_title(post: dict, channel_title: str) -> str:
    text = post["text_plain"].strip()
    if text:
        first_line = text.split("\n", 1)[0].strip()
        if len(first_line) > 100:
            first_line = first_line[:97].rstrip() + "…"
        return first_line
    if post["has_video"]:
        return f"🎥 Видео — {channel_title}"
    if post["media"]:
        return f"🖼 Фото — {channel_title}"
    return f"Пост — {channel_title}"


def render_content(post: dict) -> str:
    parts = []
    for u in post["media"]:
        parts.append(f'<p><img src="{html.escape(u)}" /></p>')
    if post["text_html"]:
        parts.append(post["text_html"])
    return "\n".join(parts) or post["text_plain"] or "(нет текста)"


def build_feed(channel: str, channel_title: str, posts: list[dict]) -> FeedGenerator:
    fg = FeedGenerator()
    fg.title(channel_title)
    fg.link(href=f"https://t.me/{channel}", rel="alternate")
    if PAGES_BASE_URL:
        fg.link(href=f"{PAGES_BASE_URL}/feeds/{channel}.xml", rel="self")
    fg.description(
        f"RSS-лента канала @{channel}, собранная из публичного веб-превью Telegram."
    )
    fg.language("ru")
    fg.generator("telegram_rss.py")

    for post in posts:
        fe = fg.add_entry(order="append")  # keep our newest-first order
        fe.guid(post["link"], permalink=True)
        fe.title(build_title(post, channel_title))
        fe.link(href=post["link"])
        fe.published(post["published"])
        fe.author(name=channel_title)
        fe.content(render_content(post), type="CDATA")
        fe.description(post["text_plain"] or build_title(post, channel_title))

    return fg


def build_combined_feed(all_entries: list[tuple]) -> None:
    fg = FeedGenerator()
    fg.title(f"{SITE_TITLE} — все каналы")
    fg.link(href="https://t.me", rel="alternate")
    if PAGES_BASE_URL:
        fg.link(href=f"{PAGES_BASE_URL}/feeds/all.xml", rel="self")
    fg.description("Объединённая лента всех отслеживаемых Telegram-каналов.")
    fg.language("ru")
    fg.generator("telegram_rss.py")

    all_entries.sort(key=lambda x: x[0], reverse=True)
    for published, channel_title, _channel, post in all_entries[:COMBINED_LIMIT]:
        fe = fg.add_entry(order="append")
        fe.guid(post["link"], permalink=True)
        fe.title(f"[{channel_title}] {build_title(post, channel_title)}")
        fe.link(href=post["link"])
        fe.published(published)
        fe.author(name=channel_title)
        fe.content(render_content(post), type="CDATA")
        fe.description(post["text_plain"] or build_title(post, channel_title))

    out_path = OUTPUT_DIR / "all.xml"
    fg.rss_file(str(out_path), pretty=True)
    print(f"  wrote {out_path} ({min(len(all_entries), COMBINED_LIMIT)} posts)")

# ---- Landing page ----------------------------------------------------------

def write_index(summary: list[tuple]) -> None:
    rows = []
    for channel, channel_title, status in summary:
        title = channel_title or f"@{channel}"
        rows.append(
            f'<tr><td><a href="https://t.me/{channel}">{html.escape(title)}</a></td>'
            f'<td><a href="feeds/{channel}.xml">{channel}.xml</a></td>'
            f'<td>{html.escape(status)}</td></tr>'
        )
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(SITE_TITLE)}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 760px;
         margin: 40px auto; padding: 0 16px; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e5e5e5; }}
  .muted {{ color: #777; font-size: .9rem; }}
</style>
</head>
<body>
<h1>{html.escape(SITE_TITLE)}</h1>
<p class="muted">Обновлено: {updated}. Сводная лента: <a href="feeds/all.xml">all.xml</a></p>
<table>
<thead><tr><th>Канал</th><th>RSS</th><th>Статус</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
</body>
</html>
"""
    Path("docs/index.html").write_text(doc, encoding="utf-8")
    print("  wrote docs/index.html")

# ---- Main ------------------------------------------------------------------

def main() -> int:
    channels = read_channels(CHANNELS_FILE)
    if not channels:
        print("No channels to process.", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    Path("docs/.nojekyll").touch()  # let Pages serve files as-is
    print(f"Processing {len(channels)} channel(s)...")

    all_entries: list[tuple] = []
    summary: list[tuple] = []
    ok = 0

    for channel in channels:
        print(f"- {channel}")
        page = fetch_channel_html(channel)
        if page is None:
            print("  skipped (could not fetch).", file=sys.stderr)
            summary.append((channel, None, "ошибка загрузки"))
            continue

        channel_title, posts = parse_posts(channel, page)
        if not posts:
            print("  no posts found (private/empty/renamed?).", file=sys.stderr)
            summary.append((channel, channel_title, "нет постов"))
            continue

        fg = build_feed(channel, channel_title, posts)
        out_path = OUTPUT_DIR / f"{channel}.xml"
        fg.rss_file(str(out_path), pretty=True)
        print(f"  wrote {out_path} ({len(posts)} posts)")
        ok += 1
        summary.append((channel, channel_title, f"{len(posts)} постов"))

        for post in posts:
            all_entries.append((post["published"], channel_title, channel, post))

    build_combined_feed(all_entries)
    write_index(summary)

    print(f"Done. {ok}/{len(channels)} channels succeeded.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
