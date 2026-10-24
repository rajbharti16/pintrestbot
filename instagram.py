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


class IGSessionError(IGError):
    """Instagram rejected our login session (expired / logged out)."""


_state = {"dead": False, "session_dead": False, "reason": "login"}
# "dead": the last API call was rejected. "session_dead": we stopped using the session.
_hooks = {"dead": None}
_SESSION_MSG = (
    "Instagram login session expire ho gaya. Bot owner ko bata diya gaya hai, "
    "thodi der baad try karo."
)


def _login_wall(r):
    """Why Instagram refused us: 'login' (session invalid), 'challenge' (needs
    verification) or None (something else). Works for redirects and JSON errors."""
    text = r.text[:600].lower()
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("location", "").lower()
        if "challenge" in loc or "checkpoint" in loc:
            return "challenge"
        if "login" in loc or "/accounts/" in loc:
            return "login"
        return None
    if r.status_code in (400, 401, 403):
        if any(k in text for k in ("challenge_required", "checkpoint_required", "consent_required")):
            return "challenge"
        if any(k in text for k in ("login_required", "logged_out")):
            return "login"
    return None


def set_dead_hook(fn):
    """bot.py registers a coroutine that DMs the owner when the session dies."""
    _hooks["dead"] = fn


def _proxy():
    """Optional: route Instagram requests through a proxy (IG_PROXY=http://user:pass@host:port)."""
    return os.getenv("IG_PROXY", "").strip() or None


@dataclass
class IGItem:
    kind: str  # video | photo
    path: str


@dataclass
class IGResult:
    items: list
    title: str
    url: str
    total: int = 1  # how many photos/videos the post really has


def _ig_ua() -> str:
    """Use the SAME user agent as the browser you logged in with (IG_USER_AGENT)."""
    return os.getenv("IG_USER_AGENT", "").strip() or UA


def _env(name: str) -> str:
    return os.getenv(name, "").strip().strip('"').strip("'")


def _cookie_dict():
    """Instagram login cookies, all from Heroku config vars:
    IG_COOKIES    = the whole cookie header copied from the browser (best)
    IG_SESSIONID  = just the sessionid value (simple)"""
    jar = {}
    for part in _env("IG_COOKIES").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                jar[k.strip()] = v.strip()
    if _env("IG_SESSIONID"):
        jar["sessionid"] = _env("IG_SESSIONID")
    return jar if jar.get("sessionid") else None


def _cookie_file():
    """cookies.txt for yt-dlp, built from the env cookies."""
    jar = _cookie_dict()
    if not jar:
        return None
    path = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for k, v in jar.items():
            f.write(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")
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
        "http_headers": {"User-Agent": _ig_ua()},
    }
    try:  # reuse the bundled ffmpeg if available (merging/remuxing)
        from pinterest import _ffmpeg_exe
        opts["ffmpeg_location"] = _ffmpeg_exe()
    except Exception:
        pass
    if cookies:
        opts["cookiefile"] = cookies
    if _proxy():
        opts["proxy"] = _proxy()

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    return (info.get("title") or info.get("description") or "").strip()


def _img_index(url: str):
    m = re.search(r"[?&]img_index=(\d+)", url)
    return int(m.group(1)) if m else None


def _only_requested(items, url: str, all_slides: bool = False):
    """Keep only what the link points to (one story / one carousel slide)."""
    m = re.search(r"/stories/[\w.]+/(\d+)", url)
    if m:
        story_id = m.group(1)
        matched = [i for i in items if story_id in os.path.basename(i.path)]
        return matched or (items if len(items) == 1 else [])
    idx = _img_index(url)
    if idx and len(items) > 1 and 1 <= idx <= len(items):
        return [items[idx - 1]]
    if not all_slides and len(items) > 1:
        return items[:1]  # link of a carousel post -> just the first photo
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


def _embed_video_url(page: str):
    m = re.search(r'video_url\\*"\s*:\s*\\*"(https:.*?)\\*"', page)
    if not m:
        return None
    u = re.sub(r"\\+u0026", "&", m.group(1)).replace("\\/", "/")
    return u.replace("\\", "")


async def _embed_fallback(url: str, folder: str):
    """Best-effort: public posts expose their media on the embed page."""
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([\w-]+)", url)
    if not m:
        return []
    embed = f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": UA}, proxy=_proxy()) as client:
        r = await client.get(embed)
        if r.status_code != 200:
            log.warning("embed page HTTP %s", r.status_code)
            return []
        page = r.text

        urls = []
        vurl = _embed_video_url(page)
        if vurl:
            urls.append(("video", vurl))
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


def _web_headers(jar) -> dict:
    h = {
        "User-Agent": _ig_ua(), "X-IG-App-ID": IG_APP_ID, "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest", "Referer": "https://www.instagram.com/",
    }
    if jar and jar.get("csrftoken"):
        h["X-CSRFToken"] = jar["csrftoken"]
    return h


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


async def _download_item(item: dict, folder: str, only=None, all_slides: bool = False):
    medias = item.get("carousel_media") or [item]
    total = len(medias)
    if total > 1:
        if only and 1 <= only <= total:
            medias = [medias[only - 1]]
        elif not all_slides:
            medias = medias[:1]  # only the first photo unless "all" was asked
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
    return out, title, total


async def _api_fetch(url: str, folder: str, jar, all_slides: bool = False):
    """Instagram's own media-info API (works best with a logged-in sessionid)."""
    mid = _media_id(url)
    if not mid:
        return [], "", 1
    web = (
        f"https://www.instagram.com/api/v1/media/{mid}/info/",
        _web_headers(jar),
    )
    mobile = (
        f"https://i.instagram.com/api/v1/media/{mid}/info/",
        {"User-Agent": MOBILE_UA, "X-IG-App-ID": IG_APP_ID},
    )
    # A browser session must only talk to the web API with the browser's user agent;
    # mixing it with the Android API looks suspicious and gets sessions logged out.
    attempts = [web] if jar else [mobile, web]
    async with httpx.AsyncClient(timeout=60, cookies=jar or None, proxy=_proxy()) as client:
        for api, headers in attempts:
            host = api.split("/")[2]
            try:
                r = await client.get(api, headers=headers)
            except httpx.HTTPError as e:
                log.warning("IG API %s error: %s", host, e)
                continue
            if r.status_code != 200:
                log.warning("IG API %s -> HTTP %s location=%s: %s", host, r.status_code,
                            r.headers.get("location", "")[:120], r.text[:150].replace("\n", " "))
                wall = _login_wall(r)
                if jar and wall:
                    _state["dead"] = True  # our session was rejected
                    _state["reason"] = wall
                continue
            try:
                item = r.json()["items"][0]
            except Exception:
                log.warning("IG API %s -> unexpected body: %s", host, r.text[:150].replace("\n", " "))
                continue
            items, title, total = await _download_item(item, folder, _img_index(url), all_slides)
            if items:
                log.info("IG API %s -> %d item(s) of %d", host, len(items), total)
                _state["dead"] = False
                return items, title, total
    return [], "", 1


def _friendly(err: str, is_story: bool, has_cookies: bool) -> str:
    low = err.lower()
    if is_story and not has_cookies:
        return "Story ke liye Instagram login chahiye (bot owner IG_SESSIONID set kare)."
    if any(k in low for k in ("login", "rate-limit", "rate limit", "cookies", "empty media")):
        if has_cookies:
            return "Instagram ne request block kar di. Session expire ho sakta hai, thodi der baad try karo."
        return "Instagram ne request block kar di (login chahiye ho sakta hai). Thodi der baad try karo."
    return "Is link se media nahi mila (private ya delete ho sakta hai)."


_sem = asyncio.Semaphore(2)  # little parallel traffic -> the login session lives longer


async def _fire_dead():
    hook = _hooks["dead"]
    if hook:
        try:
            await hook(_state.get("reason"))
        except Exception:
            log.exception("session-dead hook failed")


async def check_session():
    """True = session works, False = Instagram rejected it, None = unknown / no cookies."""
    jar = _cookie_dict()
    if not jar:
        return None
    try:
        async with httpx.AsyncClient(timeout=30, cookies=jar, proxy=_proxy()) as client:
            r = await client.get(
                "https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
                headers=_web_headers(jar),
            )
    except httpx.HTTPError as e:
        log.info("IG session check failed to run: %r", e)
        return None
    if r.status_code == 200 and '"user"' in r.text:
        return True
    wall = _login_wall(r)
    if wall:
        _state["reason"] = wall
        return False
    log.info("IG session check inconclusive: HTTP %s location=%s %s", r.status_code,
             r.headers.get("location", "")[:120], r.text[:120].replace("\n", " "))
    return None


async def watchdog_once():
    """Called periodically: notices a dead session even when nobody sends a link."""
    alive = await check_session()
    if alive is False:
        if not _state["session_dead"]:
            log.warning("Instagram session is dead")
        _state["session_dead"] = True
        await _fire_dead()
    elif alive is True and _state["session_dead"]:
        log.info("Instagram session works again")
        _state["session_dead"] = False


async def fetch_instagram(url: str, folder: str, all_slides: bool = False) -> IGResult:
    async with _sem:
        return await _fetch(url, folder, all_slides)


async def _fetch(url: str, folder: str, all_slides: bool) -> IGResult:
    is_story = "/stories/" in url
    cookies = _cookie_file()
    jar = _cookie_dict()
    if is_story and not jar:
        raise IGError(_friendly("", True, False))

    if jar and _state["session_dead"]:
        if is_story:  # stories need a login, nothing else to try
            await _fire_dead()
            raise IGSessionError(_SESSION_MSG)
        jar = cookies = None  # dead session: public posts / reels are tried without it

    err = ""
    # 1) Instagram API (with the saved session if it still works)
    _state["dead"] = False
    items, title, total = await _api_fetch(url, folder, jar, all_slides)
    if _state["dead"] and jar:
        _state["session_dead"] = True
        await _fire_dead()
        if is_story:
            raise IGSessionError(_SESSION_MSG)
        jar = cookies = None  # drop the dead session and retry as a visitor
        items, title, total = await _api_fetch(url, folder, None, all_slides)

    # 2) yt-dlp
    if not items:
        try:
            title = await asyncio.to_thread(_ytdlp, url, folder, cookies)
        except Exception as e:  # yt-dlp raises DownloadError and friends
            err = str(e)
            log.warning("yt-dlp failed: %s", err[:300])
        found = _collect(folder)
        total = len(found) or 1
        items = _only_requested(found, url, all_slides)

    # 3) public embed page (posts / reels only)
    if not items and not is_story:
        found = await _embed_fallback(url, folder)
        total = len(found) or 1
        items = _only_requested(found, url, all_slides)
        if not items:
            log.warning("embed fallback found nothing for %s", url)
    if not items:
        if _state["session_dead"]:
            raise IGSessionError(_SESSION_MSG)
        raise IGError(_friendly(err, is_story, bool(cookies)))

    return IGResult(items=items, title=title, url=url, total=total)
