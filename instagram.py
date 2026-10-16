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

    is_story = "/stories/" in url
    tmpl = "story_%(id)s.%(ext)s" if is_story else "%(autonumber)s.%(ext)s"
    opts = {
        "noplaylist": is_story,  # don't grab every story of the account
        "outtmpl": os.path.join(folder, tmpl),
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


def _img_index(url: str):
    m = re.search(r"[?&]img_index=(\d+)", url)
    return int(m.group(1)) if m else None


def _only_requested(items, url: str):
    """Keep only what the link points to (one story / one carousel slide)."""
    m = re.search(r"/stories/[\w.]+/(\d+)", url)
    if m:
        story_id = m.group(1)
        matched = [i for i in items if story_id in os.path.basename(i.path)]
        return matched or (items if len(items) == 1 else [])
    idx = _img_index(url)
    if idx and len(items) > 1 and 1 <= idx <= len(items):
        return [items[idx - 1]]
    return items


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


IG_APP_ID = "936619743392459"
MOBILE_UA = (
    "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; "
    "SM-G991B; o1s; exynos2100; en_US; 458229237)"
)
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _media_id(url: str):
    m = re.search(r"/(?:p|reel|reels|tv)/([\w-]+)", url)
    if m:
        code = m.group(1)[:11]
        if all(ch in _B64 for ch in code):
            n = 0
            for ch in code:
                n = n * 64 + _B64.index(ch)
            return n
        return None
    m = re.search(r"/stories/[\w.]+/(\d+)", url)
    return int(m.group(1)) if m else None


async def _download_item(item: dict, folder: str, only=None):
    medias = item.get("carousel_media") or [item]
    if only and len(medias) > 1 and 1 <= only <= len(medias):
        medias = [medias[only - 1]]
    caption = ((item.get("caption") or {}).get("text") or "").strip()
    limit = MAX_FILE_MB * 1024 * 1024
    out = []
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": UA}) as c:
        for i, mm in enumerate(medias[:MAX_ITEMS]):
            if mm.get("video_versions"):
                kind, u, ext = "video", mm["video_versions"][0]["url"], ".mp4"
            elif (mm.get("image_versions2") or {}).get("candidates"):
                kind, u, ext = "photo", mm["image_versions2"]["candidates"][0]["url"], ".jpg"
            else:
                continue
            path = os.path.join(folder, f"api_{i:02d}{ext}")
            size = 0
            try:
                async with c.stream("GET", u) as resp:
                    if resp.status_code != 200:
                        log.warning("IG cdn download HTTP %s", resp.status_code)
                        continue
                    with open(path, "wb") as f:
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if size > limit:
                                break
                            f.write(chunk)
            except httpx.HTTPError as e:
                log.warning("IG cdn download failed: %s", e)
                continue
            if size > limit:
                os.remove(path)
                continue
            out.append(IGItem(kind, path))
    title = caption.splitlines()[0][:150] if caption else ""
    return out, title


async def _api_fetch(url: str, folder: str, sid: str):
    """Instagram's own media-info API (works best with a logged-in sessionid)."""
    mid = _media_id(url)
    if not mid:
        return [], ""
    attempts = [
        (f"https://i.instagram.com/api/v1/media/{mid}/info/",
         {"User-Agent": MOBILE_UA, "X-IG-App-ID": IG_APP_ID}),
        (f"https://www.instagram.com/api/v1/media/{mid}/info/",
         {"User-Agent": UA, "X-IG-App-ID": IG_APP_ID,
          "X-Requested-With": "XMLHttpRequest", "Referer": "https://www.instagram.com/"}),
    ]
    cookies = {"sessionid": sid} if sid else None
    async with httpx.AsyncClient(timeout=60, cookies=cookies) as client:
        for api, headers in attempts:
            host = api.split("/")[2]
            try:
                r = await client.get(api, headers=headers)
            except httpx.HTTPError as e:
                log.warning("IG API %s error: %s", host, e)
                continue
            if r.status_code != 200:
                log.warning("IG API %s -> HTTP %s: %s", host, r.status_code, r.text[:150].replace("\n", " "))
                continue
            try:
                item = r.json()["items"][0]
            except Exception:
                log.warning("IG API %s -> unexpected body: %s", host, r.text[:150].replace("\n", " "))
                continue
            items, title = await _download_item(item, folder, _img_index(url))
            if items:
                log.info("IG API %s -> %d item(s)", host, len(items))
                return items, title
    return [], ""


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

    sid = os.getenv("IG_SESSIONID", "").strip()
    err = ""

    # 1) Instagram API (uses IG_SESSIONID if set)
    items, title = await _api_fetch(url, folder, sid)

    # 2) yt-dlp
    if not items:
        try:
            title = await asyncio.to_thread(_ytdlp, url, folder, cookies)
        except Exception as e:  # yt-dlp raises DownloadError and friends
            err = str(e)
            log.warning("yt-dlp failed: %s", err[:300])
        items = _only_requested(_collect(folder), url)

    # 3) public embed page (posts / reels only)
    if not items and not is_story:
        items = _only_requested(await _embed_fallback(url, folder), url)
        if not items:
            log.warning("embed fallback found nothing for %s", url)
    if not items:
        raise IGError(_friendly(err, is_story, bool(cookies)))

    return IGResult(items=items, title=title, url=url)
