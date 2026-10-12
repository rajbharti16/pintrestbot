"""Instagram downloader: reels, posts (photo/carousel) and stories.

- Reels / posts: public ones work without login (yt-dlp, then embed-page fallback).
- Stories / highlights: Instagram requires login, so set IG_SESSIONID in env.
"""
import asyncio
import html
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass

import httpx

from config import MAX_FILE_MB

log = logging.getLogger("pinbot")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

IG_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:(?:p|reel|reels|tv)/[\w-]+"
    r"|share/(?:p|reel)/[\w-]+"
    r"|stories/highlights/\d+"
    r"|stories/[\w.]+/\d+)[^\s]*",
    re.I,
)

VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_ITEMS = 10  # Telegram album limit


class IGError(Exception):
    pass


@dataclass
class IGItem:
    kind: str  # video | photo
    path: str


@dataclass
class IGResult:
    items: list
    title: str
    url: str


def _cookie_file():
    sid = os.getenv("IG_SESSIONID", "").strip()
    if not sid:
        return None
    path = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
    with open(path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{sid}\n")
    return path


def _ytdlp(url: str, folder: str, cookies) -> str:
    import yt_dlp

    opts = {
        "outtmpl": os.path.join(folder, "%(autonumber)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": "best",
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
        "playlistend": MAX_ITEMS,
        "http_headers": {"User-Agent": UA},
    }
    try:  # reuse the bundled ffmpeg if available (merging/remuxing)
        from pinterest import _ffmpeg_exe
        opts["ffmpeg_location"] = _ffmpeg_exe()
    except Exception:
        pass
    if cookies:
        opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    return (info.get("title") or info.get("description") or "").strip()


def _collect(folder: str):
    items = []
    for name in sorted(os.listdir(folder)):
        ext = os.path.splitext(name)[1].lower()
        path = os.path.join(folder, name)
        if ext in VIDEO_EXT:
            items.append(IGItem("video", path))
        elif ext in PHOTO_EXT:
            items.append(IGItem("photo", path))
    return items[:MAX_ITEMS]


async def _embed_fallback(url: str, folder: str):
    """Best-effort: public posts expose their media on the embed page."""
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([\w-]+)", url)
    if not m:
        return []
    embed = f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": UA}) as client:
        r = await client.get(embed)
        if r.status_code != 200:
            log.warning("embed page HTTP %s", r.status_code)
            return []
        page = r.text

        urls = []
        v = re.search(r'"video_url"\s*:\s*"([^"]+)"', page)
        if v:
            urls.append(("video", json.loads(f'"{v.group(1)}"')))
        else:
            for src in re.findall(
                r'<img[^>]*class="[^"]*EmbeddedMediaImage[^"]*"[^>]*src="([^"]+)"', page
            ):
                urls.append(("photo", html.unescape(src)))

        items = []
        for i, (kind, u) in enumerate(urls[:MAX_ITEMS]):
            path = os.path.join(folder, f"embed_{i}{'.mp4' if kind == 'video' else '.jpg'}")
            try:
                async with client.stream("GET", u) as resp:
                    if resp.status_code != 200:
                        continue
                    with open(path, "wb") as f:
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            f.write(chunk)
                items.append(IGItem(kind, path))
            except httpx.HTTPError:
                continue
        return items


def _friendly(err: str, is_story: bool, has_cookies: bool) -> str:
    low = err.lower()
    if is_story and not has_cookies:
        return "Story ke liye Instagram login chahiye (bot owner IG_SESSIONID set kare)."
    if any(k in low for k in ("login", "rate-limit", "rate limit", "cookies", "empty media")):
        if has_cookies:
            return "Instagram ne request block kar di. Session expire ho sakta hai, thodi der baad try karo."
        return "Instagram ne request block kar di (login chahiye ho sakta hai). Thodi der baad try karo."
    return "Is link se media nahi mila (private ya delete ho sakta hai)."


async def fetch_instagram(url: str, folder: str) -> IGResult:
    cookies = _cookie_file()
    is_story = "/stories/" in url
    if is_story and not cookies:
        raise IGError(_friendly("", True, False))

    title, err = "", ""
    try:
        title = await asyncio.to_thread(_ytdlp, url, folder, cookies)
    except Exception as e:  # yt-dlp raises DownloadError and friends
        err = str(e)
        log.warning("yt-dlp failed: %s", err[:300])

    items = _collect(folder)
    if not items and not is_story:
        items = await _embed_fallback(url, folder)
    if not items:
        raise IGError(_friendly(err, is_story, bool(cookies)))

    return IGResult(items=items, title=title, url=url)
  
