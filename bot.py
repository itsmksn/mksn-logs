#!/usr/bin/env python3
"""mksn-logs: post new YouTube / TikTok / Twitch items to a Telegram channel.

Runs without third-party dependencies (Python 3.9+).
Configuration is read from sources.json; runtime secrets come from env vars.
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:  # Windows consoles default to a legacy code page; logs contain emoji.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - older Python or redirected streams
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
SOURCES_PATH = os.path.join(ROOT, "sources.json")

MAX_SEEN = 200
MAX_POST_PER_SOURCE = 5
MAX_TOTAL_POSTS = 10
MAX_AGE = timedelta(days=7)

# Keep a run comfortably inside one billed minute even if a feed hangs.
TIME_BUDGET_SECONDS = 45
HTTP_TIMEOUT_SECONDS = 8

DRY_RUN = "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "1"
TEST = "--test" in sys.argv
FIRST_RUN_POST = int(os.environ.get("FIRST_RUN_POST", "1"))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

STYLE = {
    "youtube": ("🎬", "YouTube"),
    "tiktok": ("🎵", "TikTok"),
    "twitch_vod": ("🟣", "Twitch"),
    "twitch_live": ("🔴", "Twitch LIVE"),
}

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # Windows without tzdata, or an old Python
    LOCAL_TZ = timezone(timedelta(hours=2))


# --------------------------------------------------------------------------- #
# HTTP / feeds
# --------------------------------------------------------------------------- #

def http_get(url: str, timeout: int = HTTP_TIMEOUT_SECONDS) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "mksn-logs/1.0 (+https://github.com/itsmksn/mksn-logs)",
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            data = gzip.decompress(data)
        return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _child_text(el: ET.Element, name: str) -> str:
    child = _child(el, name)
    if child is None:
        return ""
    return (child.text or "").strip()


def _entry_link(entry: ET.Element) -> str:
    for child in entry:
        if _local(child.tag) == "link":
            href = (child.get("href") or "").strip()
            rel = (child.get("rel") or "alternate").strip()
            if href and rel in ("alternate", ""):
                return href
    text = _child_text(entry, "link")
    if text:
        return text
    return _child_text(entry, "guid")


def _entry_id(entry: ET.Element, link: str) -> str:
    for name in ("id", "guid"):
        value = _child_text(entry, name)
        if value:
            return value
    return link


def parse_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _entry_date(entry: ET.Element) -> datetime | None:
    for name in ("pubDate", "published", "updated", "date"):
        dt = parse_date(_child_text(entry, name))
        if dt:
            return dt
    return None


def _entry_thumb(entry: ET.Element) -> str | None:
    # YouTube: <media:thumbnail url="..."/>
    for el in entry.iter():
        if _local(el.tag) == "thumbnail":
            url = (el.get("url") or "").strip()
            if url:
                return url
    desc = (
        _child_text(entry, "description")
        or _child_text(entry, "content")
        or _child_text(entry, "summary")
    )
    if not desc:
        return None
    match = re.search(r'poster="([^"]+)"', desc) or re.search(r'<img[^>]+src="([^"]+)"', desc)
    if not match:
        return None
    return html.unescape(match.group(1)).strip() or None


def parse_feed(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    items = []
    for entry in root.iter():
        if _local(entry.tag) not in ("entry", "item"):
            continue
        link = _entry_link(entry)
        if not link:
            continue
        title = _child_text(entry, "title") or "(без назви)"
        items.append(
            {
                "id": _entry_id(entry, link),
                "title": html.unescape(re.sub(r"\s+", " ", title)).strip(),
                "link": link,
                "date": _entry_date(entry),
                "thumb": _entry_thumb(entry),
            }
        )
    return items


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict) and isinstance(state.get("sources"), dict):
                return state
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": 1, "sources": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def tg_call(method: str, params: dict) -> dict:
    if DRY_RUN:
        preview = json.dumps(params, ensure_ascii=False)
        print(f"[dry-run] {method} {preview[:400]}")
        return {"ok": True}
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"Telegram HTTP {exc.code}: {body[:200]}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result.get('description')}")
    return result


def format_item(src: dict, item: dict) -> str:
    emoji = STYLE.get(src.get("type", ""), ("📌", "source"))[0]
    title = html.escape(item["title"])[:600]
    when = ""
    if item.get("date"):
        when = " · " + item["date"].astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")
    source = html.escape(src["name"])
    if src.get("type") == "twitch_live":
        head = f"🔴 <b>Стрім почався</b>\n<b>{title}</b>"
    else:
        head = f"{emoji} <b>{title}</b>"
    return f"{head}\n\n{source}{when}\n{item['link']}"


def send_item(src: dict, item: dict) -> None:
    text = format_item(src, item)
    thumb = item.get("thumb")
    if thumb:
        try:
            tg_call(
                "sendPhoto",
                {
                    "chat_id": CHAT_ID,
                    "photo": thumb,
                    "caption": text,
                    "parse_mode": "HTML",
                },
            )
            return
        except Exception as exc:  # noqa: BLE001 - fall back to a text message
            print(f"  photo failed ({exc}); falling back to text")
    tg_call(
        "sendMessage",
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def load_sources() -> list[dict]:
    with open(SOURCES_PATH, encoding="utf-8") as f:
        sources = json.load(f)
    if not isinstance(sources, list):
        raise RuntimeError("sources.json must contain a list")
    return sources


def run_test_message() -> None:
    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")
    tg_call(
        "sendMessage",
        {
            "chat_id": CHAT_ID,
            "text": "✅ <b>mksn-logs підключено</b>\nДалі сюди будуть приходити логи нових відео та стрімів.",
            "parse_mode": "HTML",
        },
    )
    print("test message sent")


def main() -> int:
    if TEST:
        run_test_message()
        return 0

    sources = load_sources()
    state = load_state()
    now = datetime.now(timezone.utc)
    started = time.monotonic()
    posted_total = 0
    post_budget = MAX_TOTAL_POSTS

    for src in sources:
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            print("time budget exhausted; remaining sources will be checked next run")
            break

        sid = src["id"]
        source_state = state["sources"].setdefault(sid, {"initialized": False, "seen": []})
        seen = source_state.setdefault("seen", [])

        try:
            items = parse_feed(http_get(src["url"]))
        except Exception as exc:  # noqa: BLE001 - one broken feed must not stop the rest
            print(f"[{sid}] fetch failed: {exc}")
            continue

        epoch = datetime.min.replace(tzinfo=timezone.utc)
        items.sort(key=lambda i: i.get("date") or epoch, reverse=True)
        unseen = [i for i in items if i["id"] not in seen]

        if not source_state.get("initialized"):
            to_post = unseen[:FIRST_RUN_POST] if FIRST_RUN_POST > 0 else []
            source_state["initialized"] = True
        else:
            fresh = [i for i in unseen if i.get("date") and now - i["date"] <= MAX_AGE]
            to_post = fresh[:MAX_POST_PER_SOURCE]

        to_post.sort(key=lambda i: i.get("date") or now)
        failed_ids: set[str] = set()

        for item in to_post:
            if post_budget <= 0:
                failed_ids.add(item["id"])  # stay unseen, post next run
                continue
            try:
                send_item(src, item)
                post_budget -= 1
                posted_total += 1
                print(f"[{sid}] posted: {item['title'][:90]}")
                time.sleep(0.7)
            except Exception as exc:  # noqa: BLE001
                failed_ids.add(item["id"])
                print(f"[{sid}] send failed: {exc}")

        refreshed = [i["id"] for i in items if i["id"] not in failed_ids]
        source_state["seen"] = list(dict.fromkeys(refreshed + seen))[:MAX_SEEN]
        print(f"[{sid}] fetched={len(items)} unseen={len(unseen)} posted={len(to_post)}")

    save_state(state)
    print(f"done: posted {posted_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
