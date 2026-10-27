"""Instagram downloader: reels, posts (photo / carousel), stories and highlights.

How it works
------------
* Public reels / posts / photos are downloaded WITHOUT any login (post page meta
  tags, yt-dlp, embed page, re-embed services, anonymous API). Login cookies are
  never sent for them, so normal use does not wear out the session.
* Only Stories and Highlights need a login. They use the session from the Heroku
  Config Vars: IG_COOKIES (preferred, full cookie header) or IG_SESSIONID.
* If Instagram rejects the session, the bot stops using it (no repeated tries),
  tells the user to try again after ~24 hours, and DMs the owner through the hook
  bot.py registers with set_dead_hook(). Public downloads keep working.

Optional Config Vars
--------------------
IG_COOKIES        "name=value; name2=value2; ..." copied from the browser
IG_SESSIONID      just the sessionid value (used only if IG_COOKIES has no sessionid)
IG_USER_AGENT     user agent of the browser you logged in with
IG_PROXY          http://user:pass@host:port
IG_FIX_DOMAINS    re-embed services, default "kkinstagram.com,ddinstagram.com" ("" = off)
IG_LOGIN_FALLBACK 1 = if every public method fails, try once more with the login
                  (default off, so the session is kept for stories / highlights)
"""
import asyncio
import contextlib
import html
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import unquote, urljoin

import httpx

from config import MAX_FILE_MB

log = logging.getLogger("pinbot")

IG_BUILD = "2026-10-26-public-first"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
FIXER_UA = "TelegramBot (like TwitterBot)"
MOBILE_UA = (
    "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; "
    "SM-G991B; o1s; exynos2100; en_US; 458229237)"
)
IG_APP_ID = "936619743392459"
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

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

# How often (seconds) the session may be checked / the owner reminded.
ALIVE_CHECK_EVERY = 6 * 3600
DEAD_CHECK_EVERY = 6 * 3600
NOTIFY_EVERY = 6 * 3600

# ---- messages shown to users (they never contain cookies or technical details) ----
_NO_LOGIN_MSG = (
    "Story / Highlight download karne ke liye Instagram login chahiye. "
    "Bot owner ne abhi login set nahi kiya."
)
_SESSION_MSG = (
    "Instagram login session expire ho gaya hai. Story / Highlight 24 ghante baad "
    "try karo, bot owner ko bata diya gaya hai."
)
_STORY_FAIL_MSG = (
    "Ye story / highlight abhi nahi mil rahi. Wo expire ho chuki ho sakti hai, ya account "
    "private hai. Thodi der baad try karo."
)
_PUBLIC_FAIL_MSG = (
    "Ye post abhi download nahi ho pa rahi. Post private ho sakti hai ya Instagram ne "
    "abhi block kiya hai, thodi der baad try karo."
)
_MORE_SLIDES_MSG = "Is post ki baaki photos abhi nahi mil rahi."


class IGError(Exception):
    pass


class IGSessionError(IGError):
    """Instagram rejected our login session (expired / logged out)."""


_state = {
    "session_dead": False,  # Instagram rejected the login: we stop using it
    "reason": "login",      # login | challenge
    "last_check": 0.0,      # last watchdog check
    "last_notify": 0.0,     # last time the owner was told
}
_hooks = {"dead": None}
_watchdog_task = None
_sem = asyncio.Semaphore(2)  # little parallel traffic -> the login lives longer


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


# ======================================================================
# Config helpers (everything comes from Heroku Config Vars)
# ======================================================================
def _env(name: str) -> str:
    return os.getenv(name, "").strip().strip('"').strip("'")


def _ig_ua() -> str:
    """Use the SAME user agent as the browser you logged in with (IG_USER_AGENT)."""
    return os.getenv("IG_USER_AGENT", "").strip() or UA


def _proxy():
    """Optional proxy for every Instagram request (IG_PROXY=http://user:pass@host:port)."""
    return os.getenv("IG_PROXY", "").strip() or None


def _login_fallback_enabled() -> bool:
    return os.getenv("IG_LOGIN_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")


def _parse_cookie_string(raw: str) -> dict:
    """'a=1; b=2' (optionally starting with 'Cookie:') -> {'a': '1', 'b': '2'}.
    The first '=' splits name and value, so values may contain '=' and quotes."""
    raw = (raw or "").strip().strip('"').strip("'").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:]
    jar = {}
    for part in re.split(r"[;\r\n]+", raw):
        name, sep, value = part.partition("=")
        name = name.strip()
        if sep and name and not re.search(r"\s", name):
            jar[name] = value.strip()
    return jar


def _cookie_dict():
    """Login cookies from the environment, or None when no sessionid is configured.
    IG_COOKIES wins; IG_SESSIONID is only used when IG_COOKIES has no sessionid."""
    jar = _parse_cookie_string(os.getenv("IG_COOKIES", ""))
    if not jar.get("sessionid"):
        sid = _env("IG_SESSIONID")
        if sid:
            jar["sessionid"] = sid
    return jar if jar.get("sessionid") else None


_COOKIE_RE = re.compile(
    r"(sessionid|csrftoken|ds_user_id|ig_did|mid|datr|rur|shbid|shbts)=[^;\s]+", re.I
)


def _redact(text) -> str:
    """Remove cookie / session values from anything that is about to be logged."""
    s = str(text)
    for v in (_cookie_dict() or {}).values():
        for variant in {v, unquote(v)}:
            if len(variant) >= 6:
                s = s.replace(variant, "***")
    return _COOKIE_RE.sub(lambda m: m.group(1) + "=***", s)


@contextlib.contextmanager
def _cookie_tempfile(jar):
    """cookies.txt for yt-dlp in a private temp file (mode 0600), deleted afterwards."""
    if not jar:
        yield None
        return
    fd, path = tempfile.mkstemp(prefix="igck_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for k, v in jar.items():
                k = re.sub(r"[\t\r\n]", "", k)
                v = re.sub(r"[\t\r\n]", "", v)
                f.write(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _rm_quiet(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# ======================================================================
# Reading Instagram's answers
# ======================================================================
def _login_wall(r):
    """Why Instagram refused us: 'login' (session invalid), 'challenge' (needs
    verification) or None (something else). Works for redirects and JSON errors."""
    text = r.text[:600].lower()
    final = str(getattr(r, "url", "")).lower()
    if "/challenge" in final or "checkpoint" in final:
        return "challenge"
    if "/accounts/login" in final:
        return "login"
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


def _clean_title(raw: str, limit: int = 150) -> str:
    """'user on Instagram: "caption..."' -> 'user: caption...' cut at a word boundary."""
    t = html.unescape(raw or "").strip()
    user = ""
    m = re.match(r'^(.*?)\s+on Instagram:\s*["\u201c]?(.*)$', t, re.S)
    if m:
        user, t = m.group(1).strip(), m.group(2)
    t = re.sub(r"^(Video|Photo|Reel)s? by .*$", "", t.strip())  # yt-dlp's generic titles
    t = re.sub(r'["\u201d]\s*$', "", t.strip())                  # closing quote
    t = re.sub(r"\s+", " ", t).strip()
    if user and t:
        t = f"{user}: {t}"
    elif user:
        t = user
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(",;:-\u2013 ") + "\u2026"
    return t


def _img_index(url: str):
    m = re.search(r"[?&]img_index=(\d+)", url)
    return int(m.group(1)) if m else None


def _only_requested(items, url: str, all_slides: bool = False):
    """Keep only what the link points to (one story / one carousel slide)."""
    if "/stories/highlights/" in url:
        return items[:MAX_ITEMS]  # a highlight is a set of stories: keep them all
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
    if not os.path.isdir(folder):
        return items
    for name in sorted(os.listdir(folder)):
        ext = os.path.splitext(name)[1].lower()
        path = os.path.join(folder, name)
        if ext in VIDEO_EXT:
            items.append(IGItem("video", path))
        elif ext in PHOTO_EXT:
            items.append(IGItem("photo", path))
    return items[:MAX_ITEMS]


def _looks_like_video(page: str) -> bool:
    return any(k in page for k in (
        '"is_video":true', '\\"is_video\\":true', "GraphVideo", "XDTGraphVideo", "video_url",
    ))


# ======================================================================
# Method 1: yt-dlp
# ======================================================================
class _YtdlpLogger:
    """Keeps yt-dlp quiet and makes sure nothing secret reaches the logs."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        log.warning("yt-dlp: %s", _redact(msg)[:300])


def _ytdlp(url: str, folder: str, jar) -> str:
    import yt_dlp

    os.makedirs(folder, exist_ok=True)
    story = "/stories/" in url
    single_story = bool(re.search(r"/stories/(?!highlights/)[\w.]+/\d+", url))
    tmpl = "story_%(id)s.%(ext)s" if story else "%(autonumber)s.%(ext)s"
    opts = {
        "noplaylist": single_story,  # one story, not every story of the account
        "outtmpl": os.path.join(folder, tmpl),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlpLogger(),
        "format": "best",
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
        "playlistend": MAX_ITEMS,
        "http_headers": {"User-Agent": _ig_ua()},
    }
    try:  # reuse the bundled ffmpeg if available (merging / remuxing)
        from pinterest import _ffmpeg_exe
        opts["ffmpeg_location"] = _ffmpeg_exe()
    except Exception:
        pass
    if _proxy():
        opts["proxy"] = _proxy()

    with _cookie_tempfile(jar) as cookiefile:  # file exists only while yt-dlp runs
        if cookiefile:
            opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
    return (info.get("title") or info.get("description") or "").strip()


async def _run_ytdlp(url: str, folder: str, jar, all_slides: bool = False):
    """-> (items, title, total, error_text). Never raises."""
    sub = os.path.join(folder, "yt")  # own folder so other methods' files don't mix in
    title, err = "", ""
    try:
        title = await asyncio.to_thread(_ytdlp, url, sub, jar)
    except Exception as e:  # yt-dlp raises DownloadError and friends
        err = _redact(e)
    found = _collect(sub)
    return _only_requested(found, url, all_slides), _clean_title(title), len(found) or 1, err


# ======================================================================
# Method 2: the post page's own meta tags (and re-embed services)
# ======================================================================
async def _html_fetch(url: str, folder: str, jar, base: str = "https://www.instagram.com",
                      ua: str = None):
    """Read a post page's meta tags: og:video / og:image describe exactly this post.
    A thumbnail is never returned for a video post or reel."""
    m = re.search(r"instagram\.com/(p|reel|reels|tv)/([\w-]+)", url)
    if not m:
        return [], "", 1
    ptype = "reel" if m.group(1) in ("reel", "reels") else m.group(1)
    is_video_url = ptype in ("reel", "tv")
    page_url = f"{base}/{ptype}/{m.group(2)}/"
    agent = ua or _ig_ua()
    headers = {
        "User-Agent": agent, "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if not ua:
        headers.update({"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none"})
    host = base.split("//")[1]
    async with httpx.AsyncClient(timeout=60, cookies=jar or None, proxy=_proxy(),
                                 follow_redirects=True, max_redirects=4) as client:
        try:
            r = await client.get(page_url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("IG page %s error: %s", host, _redact(repr(e)))
            return [], "", 1
    page = r.text
    if r.status_code != 200:
        log.warning("IG page %s -> HTTP %s final=%s", host, r.status_code, str(r.url)[:120])
        return [], "", 1

    def meta(prop):
        mm = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]+)"' % re.escape(prop), page)
        return html.unescape(mm.group(1)) if mm else None

    video = meta("og:video") or meta("og:video:secure_url")
    image = meta("og:image")
    if video:
        kind, media_url = "video", urljoin(page_url, video)
    elif image and not (is_video_url or "video" in (meta("og:type") or "").lower()
                        or _looks_like_video(page)):
        kind, media_url = "photo", urljoin(page_url, image)
    else:
        what = "only a thumbnail of a video" if image else "no og:video/og:image"
        log.info("IG page %s has %s (%d bytes)", host, what, len(page))
        return [], "", 1

    title = _clean_title(meta("og:title") or "")
    path = os.path.join(folder, "page_00" + (".mp4" if kind == "video" else ".jpg"))
    limit = MAX_FILE_MB * 1024 * 1024
    size = 0
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": agent},
                                 follow_redirects=True) as c:
        try:
            async with c.stream("GET", media_url) as resp:
                ctype = resp.headers.get("content-type", "").lower()
                if resp.status_code != 200 or "text/html" in ctype:
                    log.warning("IG page %s media -> HTTP %s %s", host, resp.status_code, ctype)
                    return [], "", 1
                with open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        size += len(chunk)
                        if size > limit:
                            _rm_quiet(path)
                            return [], "", 1
                        f.write(chunk)
        except httpx.HTTPError as e:
            log.warning("IG page %s media download failed: %s", host, _redact(repr(e)))
            _rm_quiet(path)
            return [], "", 1
    if size < 5_000:
        _rm_quiet(path)
        return [], "", 1
    log.info("IG page %s -> %s via og tags", host, kind)
    return [IGItem(kind, path)], title, 1


async def _fixer_fetch(url: str, folder: str):
    """Public re-embed services (made for Telegram / Discord previews). They are unofficial
    and may be offline: IG_FIX_DOMAINS=kkinstagram.com,ddinstagram.com  ("" switches off)."""
    raw = os.getenv("IG_FIX_DOMAINS", "kkinstagram.com,ddinstagram.com")
    for d in [x.strip() for x in raw.split(",") if x.strip()]:
        items, title, total = await _html_fetch(url, folder, None, base=f"https://{d}", ua=FIXER_UA)
        if items:
            return items, title, total
    return [], "", 1


# ======================================================================
# Method 3: the embed page
# ======================================================================
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
    reel = bool(re.search(r"instagram\.com/(?:reel|reels|tv)/", url))
    limit = MAX_FILE_MB * 1024 * 1024
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": UA}, proxy=_proxy()) as client:
        urls = []
        for suffix in ("embed/captioned/", "embed/"):
            try:
                r = await client.get(f"https://www.instagram.com/p/{m.group(1)}/{suffix}")
            except httpx.HTTPError as e:
                log.info("embed page error: %s", _redact(repr(e)))
                continue
            if r.status_code != 200:
                log.info("embed page %s -> HTTP %s", suffix, r.status_code)
                continue
            page = r.text
            vurl = _embed_video_url(page)
            if vurl:
                urls = [("video", vurl)]
            elif reel or _looks_like_video(page):
                log.info("embed page only has a thumbnail of a video, not sending it")
            else:
                urls = [("photo", html.unescape(src)) for src in re.findall(
                    r'<img[^>]*class="[^"]*EmbeddedMediaImage[^"]*"[^>]*src="([^"]+)"', page)]
            if urls:
                break

        items = []
        for i, (kind, u) in enumerate(urls[:MAX_ITEMS]):
            path = os.path.join(folder, f"embed_{i}{'.mp4' if kind == 'video' else '.jpg'}")
            size = 0
            try:
                async with client.stream("GET", u) as resp:
                    if resp.status_code != 200:
                        continue
                    with open(path, "wb") as f:
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if size > limit:
                                break
                            f.write(chunk)
            except httpx.HTTPError:
                _rm_quiet(path)
                continue
            if size > limit or size < 2_000:
                _rm_quiet(path)
                continue
            items.append(IGItem(kind, path))
        return items


# ======================================================================
# Method 4: Instagram's media-info API (anonymous, or with the login)
# ======================================================================
def _web_headers(jar) -> dict:
    h = {
        "User-Agent": _ig_ua(), "X-IG-App-ID": IG_APP_ID, "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest", "Referer": "https://www.instagram.com/",
        "Accept-Language": "en-US,en;q=0.9", "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
    }
    if jar and jar.get("csrftoken"):
        h["X-CSRFToken"] = jar["csrftoken"]
    return h


def _media_id(url: str):
    if "/share/" in url:  # a share token is not a shortcode
        return None
    m = re.search(r"/(?:p|reel|reels|tv)/([\w-]+)", url)
    if m:
        code = m.group(1)[:11]
        if all(ch in _B64 for ch in code):
            n = 0
            for ch in code:
                n = n * 64 + _B64.index(ch)
            return n
        return None
    m = re.search(r"/stories/(?!highlights/)[\w.]+/(\d+)", url)
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
            elif (mm.get("image_versions2") or {}).get("candidates") and mm.get("media_type") != 2:
                kind, u, ext = "photo", mm["image_versions2"]["candidates"][0]["url"], ".jpg"
            else:
                continue  # never turn a video into its thumbnail
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
                log.warning("IG cdn download failed: %s", _redact(repr(e)))
                _rm_quiet(path)
                continue
            if size > limit:
                _rm_quiet(path)
                continue
            out.append(IGItem(kind, path))
    title = _clean_title(caption.splitlines()[0]) if caption else ""
    return out, title, total


async def _api_fetch(url: str, folder: str, jar, all_slides: bool = False):
    """-> (items, title, total, wall). `wall` is 'login' / 'challenge' when Instagram
    turned the logged-in request away (only possible when `jar` is given)."""
    mid = _media_id(url)
    if not mid:
        return [], "", 1, None
    web = (f"https://www.instagram.com/api/v1/media/{mid}/info/", _web_headers(jar))
    mobile = (f"https://i.instagram.com/api/v1/media/{mid}/info/",
              {"User-Agent": MOBILE_UA, "X-IG-App-ID": IG_APP_ID})
    # A browser session must only talk to the web API with the browser's user agent;
    # mixing it with the Android API looks suspicious and gets sessions logged out.
    attempts = [web] if jar else [mobile, web]
    wall = None
    async with httpx.AsyncClient(timeout=60, cookies=jar or None, proxy=_proxy(),
                                 follow_redirects=True, max_redirects=4) as client:
        for api, headers in attempts:
            host = api.split("/")[2]
            try:
                r = await client.get(api, headers=headers)
            except httpx.HTTPError as e:
                log.warning("IG API %s error: %s", host, _redact(repr(e)))
                continue
            if r.status_code != 200:
                log.info("IG API %s -> HTTP %s final=%s location=%s: %s", host, r.status_code,
                         str(r.url)[:120], r.headers.get("location", "")[:120],
                         r.text[:150].replace("\n", " "))
                if jar:
                    wall = _login_wall(r) or wall
                continue
            try:
                item = r.json()["items"][0]
            except Exception:
                log.info("IG API %s -> not JSON (final=%s): %s", host, str(r.url)[:120],
                         r.text[:100].replace("\n", " "))
                if jar:
                    wall = _login_wall(r) or wall
                continue
            items, title, total = await _download_item(item, folder, _img_index(url), all_slides)
            if items:
                log.info("IG API %s -> %d item(s) of %d", host, len(items), total)
                return items, title, total, None
    return [], "", 1, wall


# ======================================================================
# Session state, session check, watchdog
# ======================================================================
def set_dead_hook(fn):
    """bot.py registers a coroutine `fn(reason)` that DMs the owner when the session dies."""
    _hooks["dead"] = fn
    log.info("instagram.py build %s ready", IG_BUILD)


async def _fire_dead():
    _state["last_notify"] = time.time()
    hook = _hooks["dead"]
    if hook:
        try:
            await hook(_state.get("reason"))
        except Exception:
            log.exception("session-dead hook failed")


async def _mark_dead(reason):
    """Instagram rejected the login: stop using it and tell the owner."""
    first = not _state["session_dead"]
    _state["session_dead"] = True
    _state["reason"] = reason or "login"
    if first:
        log.warning("Instagram session is dead (%s)", _state["reason"])
    if first or time.time() - _state["last_notify"] >= NOTIFY_EVERY:
        await _fire_dead()


async def _remind_dead():
    """Someone asked for a story while the session is dead: nudge the owner, rarely."""
    if time.time() - _state["last_notify"] >= NOTIFY_EVERY:
        await _fire_dead()


async def check_session():
    """True = session works, False = Instagram rejected it, None = unknown / no cookies.
    Network errors and unclear answers are 'unknown', never 'expired'."""
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
        log.info("IG session check failed to run: %s", _redact(repr(e)))
        return None
    if r.status_code == 200 and '"user"' in r.text:
        if _state["session_dead"]:
            log.info("Instagram session works again")
        _state["session_dead"] = False
        _state["last_notify"] = 0.0
        return True
    wall = _login_wall(r)
    if wall:
        _state["reason"] = wall
        return False
    log.info("IG session check inconclusive: HTTP %s location=%s %s", r.status_code,
             r.headers.get("location", "")[:120], r.text[:120].replace("\n", " "))
    return None


async def _confirm_dead(wall) -> bool:
    """A refusal on one request is confirmed with a separate session check before the
    session is declared dead (one odd answer must not lock stories for everybody)."""
    if await check_session() is False:
        await _mark_dead(_state["reason"] or wall)
        return True
    return False


async def watchdog_once(force: bool = False):
    """Check the session now. Rate limited, so calling it often (or from two places)
    cannot flood Instagram: at most one real check every few hours."""
    if not _cookie_dict():
        return
    now = time.time()
    wait = DEAD_CHECK_EVERY if _state["session_dead"] else ALIVE_CHECK_EVERY
    if not force and now - _state["last_check"] < wait:
        return
    _state["last_check"] = now
    if await check_session() is False:
        await _mark_dead(_state["reason"])


def start_watchdog(interval: int = 3600, first_delay: int = 60):
    """Schedule the background session check ONCE. Calling it again is harmless
    (returns the task that is already running)."""
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        return _watchdog_task

    async def _loop():
        await asyncio.sleep(first_delay)
        while True:
            try:
                await watchdog_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Instagram watchdog failed")
            await asyncio.sleep(interval)

    _watchdog_task = asyncio.create_task(_loop())
    return _watchdog_task


def stop_watchdog():
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        _watchdog_task.cancel()
    _watchdog_task = None


# ======================================================================
# The download flows
# ======================================================================
async def _attempt(name: str, coro):
    """Run one method; if it breaks, log it (without secrets) and let the next one try."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("IG %s failed: %s", name, _redact(repr(e))[:200])
        return [], "", 1


async def _resolve_share(url: str) -> str:
    """instagram.com/share/reel/XYZ -> the real /reel/<shortcode>/ link."""
    if "/share/" not in url:
        return url
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, max_redirects=5,
                                     proxy=_proxy(), headers={"User-Agent": _ig_ua()}) as c:
            r = await c.get(url)
        final = str(r.url)
        if re.search(r"instagram\.com/(?:p|reel|reels|tv)/[\w-]+", final):
            return final
    except httpx.HTTPError as e:
        log.info("share link could not be resolved: %s", _redact(repr(e)))
    return url


async def _st_api(url, folder, jar, all_slides):
    items, title, total, _wall = await _api_fetch(url, folder, jar, all_slides)
    return items, title, total


async def _st_ytdlp(url, folder, jar, all_slides):
    items, title, total, _err = await _run_ytdlp(url, folder, jar, all_slides)
    return items, title, total


async def _st_embed(url, folder, all_slides):
    found = await _embed_fallback(url, folder)
    return _only_requested(found, url, all_slides), "", len(found) or 1


async def _fetch_public(url: str, folder: str, all_slides: bool):
    """Public reels / posts / photos. No login is used unless IG_LOGIN_FALLBACK is on
    and every public method failed. -> (items, title, total, resolved_url)"""
    url = await _resolve_share(url)
    m = re.search(r"instagram\.com/(p|reel|reels|tv)/", url)
    ptype = m.group(1) if m else ""

    stages = []
    if all_slides:  # the API is the only method that lists every slide
        stages.append(("API", lambda: _st_api(url, folder, None, True)))
    stages += [
        ("page", lambda: _html_fetch(url, folder, None)),
        ("page-crawler", lambda: _html_fetch(url, folder, None, ua=CRAWLER_UA)),
    ]
    if ptype == "p":  # photo posts: yt-dlp rarely helps, so the embed page goes first
        stages += [
            ("embed", lambda: _st_embed(url, folder, all_slides)),
            ("yt-dlp", lambda: _st_ytdlp(url, folder, None, all_slides)),
            ("re-embed", lambda: _fixer_fetch(url, folder)),
        ]
    else:
        stages += [
            ("yt-dlp", lambda: _st_ytdlp(url, folder, None, all_slides)),
            ("re-embed", lambda: _fixer_fetch(url, folder)),
            ("embed", lambda: _st_embed(url, folder, all_slides)),
        ]
    if not all_slides:
        stages.append(("API", lambda: _st_api(url, folder, None, False)))

    for name, make in stages:
        items, title, total = await _attempt(name, make())
        if items:
            log.info("IG public download via %s", name)
            return items, title, total, url

    # Last resort, only when the owner switched it on: use the login once.
    jar = _cookie_dict()
    if _login_fallback_enabled() and jar and not _state["session_dead"]:
        items, title, total, wall = await _api_fetch(url, folder, jar, all_slides)
        if wall and await _confirm_dead(wall):
            jar = None
        if not items and jar:
            items, title, total = await _attempt("page+login", _html_fetch(url, folder, jar))
        if not items and jar:
            items, title, total, _err = await _run_ytdlp(url, folder, jar, all_slides)
        if items:
            log.info("IG public download via login fallback")
            return items, title, total, url

    raise IGError(_PUBLIC_FAIL_MSG)


async def _fetch_login_media(url: str, folder: str):
    """Stories and Highlights: the only place where the login session is used."""
    jar = _cookie_dict()
    if not jar:
        raise IGError(_NO_LOGIN_MSG)
    if _state["session_dead"]:  # known bad: no more requests with it
        await _remind_dead()
        raise IGSessionError(_SESSION_MSG)

    items, title, total = [], "", 1
    if "/stories/highlights/" not in url:  # single story: try the API first
        items, title, total, wall = await _api_fetch(url, folder, jar)
        if wall and await _confirm_dead(wall):
            raise IGSessionError(_SESSION_MSG)
    if not items:
        items, title, total, err = await _run_ytdlp(url, folder, jar)
        if not items and re.search(r"login|log in|cookies|rate.?limit", err, re.I):
            if await _confirm_dead(None):
                raise IGSessionError(_SESSION_MSG)
    if not items:
        raise IGError(_STORY_FAIL_MSG)
    return items, title, total, url


async def fetch_instagram(url: str, folder: str, all_slides: bool = False) -> IGResult:
    """Download what the link points to. Raises IGError (clear Hindi message) on failure."""
    async with _sem:
        if "/stories/" in url:
            items, title, total, final = await _fetch_login_media(url, folder)
        else:
            items, title, total, final = await _fetch_public(url, folder, all_slides)
    if all_slides and "/stories/" not in url and len(items) < 2:
        raise IGError(_MORE_SLIDES_MSG)
    return IGResult(items=items, title=title, url=final, total=total)
