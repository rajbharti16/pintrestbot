"""YouTube downloader: video and MP3.

Download order:
  1) api.riteshyt.in  /download                (direct file, quality param)
  2) api.riteshyt.in  /api/platform/extract    (stream URL)
  3) yt-dlp (Heroku IP pe cookies/proxy ke bina aksar fail hota hai)

Flow: user sends a link -> bot shows title + thumbnail with quality buttons ->
user taps a button -> bot downloads and sends the file.

Config Vars:
  API_URL   default https://api.riteshyt.in
  API_KEY   tumhari API key (code me hard-code mat karo)
Optional:
  YT_COOKIES_B64, YT_PROXY, YT_MAX_MIN (default 60), YT_BIG_MAX_MB
"""
import asyncio
import base64
import html
import json
import logging
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx

from config import API_HASH, API_ID, MAX_FILE_MB, MONGO_DB, MONGO_URI, eid, em
from tg import btn

log = logging.getLogger("pinbot")

YT_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]*?v=|shorts/|live/)|youtu\.be/)[\w-]{11}[^\s]*",
    re.I,
)
ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})")
MAX_MIN = int(os.getenv("YT_MAX_MIN", "60"))
API_URL = os.getenv("API_URL", "https://api.riteshyt.in").strip().rstrip("/")
API_KEY = os.getenv("API_KEY", "").strip()
BIG_MAX_MB = int(os.getenv("YT_BIG_MAX_MB", "1000" if (API_ID and API_HASH) else "300"))

FORMATS = {"v360": 360, "v720": 720, "mp3": 0}


class YTError(Exception):
    pass


@dataclass
class YTInfo:
    id: str
    title: str
    uploader: str
    duration: int
    thumbnail: str


@dataclass
class YTFile:
    path: str
    kind: str  # video | audio
    title: str
    uploader: str
    duration: int
    width: int
    height: int


def video_id(url: str):
    m = ID_RE.search(url)
    return m.group(1) if m else None


# ---------- UI helpers ----------
def _fmt_duration(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def menu_text(info: YTInfo) -> str:
    bits = []
    if info.uploader:
        bits.append(html.escape(info.uploader[:60]))
    if info.duration:
        bits.append(_fmt_duration(info.duration))
    meta = f"{em('link', '🔗')} {'  •  '.join(bits)}\n" if bits else ""
    return (
        f"{em('pin', '📌')} <b>{html.escape(info.title[:200])}</b>\n"
        f"{meta}\n"
        f"Quality choose karo:"
    )


def menu_kb(vid: str) -> dict:
    return {
        "inline_keyboard": [
            [
                btn("360p", cb=f"yt:v360:{vid}", style="primary", emoji_id=eid("fire")),
                btn("720p HD", cb=f"yt:v720:{vid}", style="primary", emoji_id=eid("fire")),
            ],
            [btn("MP3 Audio", cb=f"yt:mp3:{vid}", style="success", emoji_id=eid("ok"))],
            [btn("Cancel", cb=f"yt:x:{vid}", style="danger", emoji_id=eid("delete"))],
        ]
    }


# ---------- yt-dlp plumbing (cookies etc.) ----------
ENV_COOKIE_PATH = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
OWNER_COOKIE_PATH = os.path.join(tempfile.gettempdir(), "yt_cookies_owner.txt")


def _to_text(data: bytes):
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def normalize_cookies(text: str, domains=("youtube.com", "google.com")):
    """Repair a cookies.txt into strict Netscape format. Returns (text, count) or (None, 0)."""
    lines = ["# Netscape HTTP Cookie File"]
    n = 0
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s or (s.startswith("#") and not s.startswith("#HttpOnly_")):
            continue
        parts = line.strip("\n").split("\t")
        if len(parts) != 7:
            parts = s.split(None, 6)
        if (
            len(parts) != 7
            or parts[1].strip().upper() not in ("TRUE", "FALSE")
            or parts[3].strip().upper() not in ("TRUE", "FALSE")
            or not parts[4].strip().isdigit()
        ):
            continue
        lines.append("\t".join(p.strip() for p in parts))
        n += 1
    if n and any(d in l for l in lines for d in domains):
        return "\n".join(lines) + "\n", n
    return None, 0


def _cookie_file():
    """Cookies saved via /ytcookies win; otherwise YT_COOKIES_B64 (base64 or plain)."""
    if os.path.exists(OWNER_COOKIE_PATH):
        return OWNER_COOKIE_PATH
    raw = os.getenv("YT_COOKIES_B64", "").strip()
    if not raw:
        return None
    try:
        data = base64.b64decode(re.sub(r"\s+", "", raw), validate=True)
    except Exception:
        data = raw.replace("\\n", "\n").replace("\\t", "\t").encode()
    text = _to_text(data)
    clean, _ = normalize_cookies(text) if text else (None, 0)
    if not clean:
        log.warning("YT_COOKIES_B64 galat hai, ignore kar raha hu.")
        return None
    with open(ENV_COOKIE_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(clean)
    return ENV_COOKIE_PATH


_settings_col = None


async def _settings():
    global _settings_col
    if not MONGO_URI:
        return None
    if _settings_col is None:
        from pymongo import AsyncMongoClient
        _settings_col = AsyncMongoClient(MONGO_URI)[MONGO_DB]["settings"]
    return _settings_col


def _write_owner_cookies(clean: str):
    with open(OWNER_COOKIE_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(clean)


async def save_cookies(raw: bytes) -> int:
    """Owner uploads a cookies.txt through the bot (/ytcookies)."""
    text = _to_text(raw)
    clean, n = normalize_cookies(text) if text else (None, 0)
    if not clean:
        raise YTError("Ye valid cookies.txt nahi lag rahi. youtube.com ki Netscape format cookies chahiye.")
    _write_owner_cookies(clean)
    col = await _settings()
    if col is not None:
        await col.update_one({"_id": "yt_cookies"}, {"$set": {"data": clean}}, upsert=True)
    return n


async def load_cookies():
    """On startup: restore cookies saved earlier with /ytcookies."""
    col = await _settings()
    if col is None:
        return
    doc = await col.find_one({"_id": "yt_cookies"})
    if doc and doc.get("data"):
        _write_owner_cookies(doc["data"])
        log.info("YouTube cookies restored from database")


def _ffmpeg_dir():
    """yt-dlp wants a dir containing `ffmpeg`; reuse the pip-bundled binary."""
    if shutil.which("ffmpeg"):
        return None
    try:
        from pinterest import _ffmpeg_exe
        exe = _ffmpeg_exe()
    except Exception:
        return None
    d = os.path.join(tempfile.gettempdir(), "ytbin")
    os.makedirs(d, exist_ok=True)
    link = os.path.join(d, "ffmpeg")
    if not os.path.exists(link):
        try:
            os.symlink(exe, link)
        except OSError:
            return None
    return d


def _base_opts() -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "noplaylist": True, "socket_timeout": 30, "retries": 3,
    }
    cookies = _cookie_file()
    if cookies:
        opts["cookiefile"] = cookies
    proxy = os.getenv("YT_PROXY", "").strip()
    if proxy:
        opts["proxy"] = proxy
    ff = _ffmpeg_dir()
    if ff:
        opts["ffmpeg_location"] = ff
    return opts


def format_selector(fmt: str) -> str:
    if fmt == "mp3":
        return "bestaudio[ext=m4a]/bestaudio/best"
    h = FORMATS[fmt]
    return (
        f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]"
        f"/b[height<={h}][ext=mp4]"
        f"/bv*[height<={h}]+ba"
        f"/b[height<={h}]/b"
    )


def _friendly(err: str) -> str:
    low = err.lower()
    if "codec can't decode" in low or "cookie" in low:
        return "Cookies file kharab hai. Owner bot ko /ytcookies ke saath cookies.txt file bheje."
    if "not a bot" in low or "sign in to confirm" in low:
        return "Download abhi nahi ho paya (YouTube ne bot detect kiya). Thodi der baad try karo."
    if "javascript runtime" in low or "challenge" in low or "ejs" in low:
        return "Server pe JS runtime (deno) nahi mila, owner ko requirements me `deno` aur `yt-dlp[default]` daalna hoga."
    if "private" in low or "unavailable" in low or "removed" in low or "terminated" in low:
        return "Ye video private ya unavailable hai."
    if "age" in low and "restrict" in low:
        return "Ye video age-restricted hai, cookies ke bina download nahi hoga."
    if "region" in low or "not available in your country" in low:
        return "Ye video is region me available nahi hai."
    return "YouTube se video nahi mil paya, thodi der baad try karo."


def _info_sync(url: str) -> dict:
    import yt_dlp

    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ---------- API helpers ----------
def _api_params(**extra) -> dict:
    params = dict(extra)
    if API_KEY:
        params["api_key"] = API_KEY
    return params


def _abs_api_url(url: str) -> str:
    """Relative stream links ko poora karo aur API key jodo (sirf API host ke liye)."""
    if url.startswith("/"):
        url = urljoin(API_URL + "/", url.lstrip("/"))
    parsed = urlparse(url)
    if parsed.netloc == urlparse(API_URL).netloc and API_KEY:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if not query.get("api_key"):
            query["api_key"] = [API_KEY]
            parsed = parsed._replace(query=urlencode(query, doseq=True))
            url = urlunparse(parsed)
    return url


def _thumb(vid: str) -> str:
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


async def _api_details(vid: str):
    if not API_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(
                f"{API_URL}/details",
                params=_api_params(link=f"https://www.youtube.com/watch?v={vid}"),
            )
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, dict) and d.get("title"):
                return d
        log.warning("API /details -> HTTP %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("API /details failed: %r", e)
    return None


async def _api_info(vid: str):
    d = await _api_details(vid)
    if not d:
        return None
    return YTInfo(
        id=d.get("vidid") or vid,
        title=d.get("title") or "YouTube video",
        uploader="",
        duration=int(d.get("duration_sec") or 0),
        thumbnail=d.get("thumbnail") or _thumb(vid),
    )


async def _oembed_info(vid: str):
    """Public oEmbed: title/channel/thumbnail without any login (duration unknown)."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://www.youtube.com/oembed",
                params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
            )
        if r.status_code == 200:
            d = r.json()
            return YTInfo(
                id=vid, title=d.get("title") or "YouTube video",
                uploader=d.get("author_name") or "", duration=0,
                thumbnail=d.get("thumbnail_url") or _thumb(vid),
            )
    except Exception as e:
        log.info("oEmbed failed: %r", e)
    return None


def _check_limits(info: YTInfo) -> YTInfo:
    if info.duration and info.duration > MAX_MIN * 60:
        raise YTError(f"Video {MAX_MIN} minute se lamba hai.")
    return info


async def get_info(url: str) -> YTInfo:
    vid = video_id(url)

    if vid:
        # 1) API /details  2) public oEmbed  (dono tez, login nahi chahiye)
        if API_URL:
            info = await _api_info(vid)
            if info:
                return _check_limits(info)
        info = await _oembed_info(vid)
        if info:
            return info

    # 3) yt-dlp (sabse last, Heroku IP pe dheema/fail ho sakta hai)
    try:
        data = await asyncio.to_thread(_info_sync, url)
    except Exception as e:
        log.warning("yt-dlp info failed: %s", str(e)[:300])
        raise YTError(_friendly(str(e)))
    if not data:
        raise YTError("Video ki info nahi mili.")
    if data.get("is_live"):
        raise YTError("Live stream download nahi ho sakti.")
    return _check_limits(YTInfo(
        id=data["id"],
        title=data.get("title") or "YouTube video",
        uploader=data.get("uploader") or data.get("channel") or "",
        duration=int(data.get("duration") or 0),
        thumbnail=data.get("thumbnail") or "",
    ))


# ---------- yt-dlp download (last resort) ----------
def _download_sync(vid: str, fmt: str, folder: str) -> dict:
    import yt_dlp

    opts = _base_opts()
    opts.update({
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "max_filesize": BIG_MAX_MB * 1024 * 1024,
        "format": format_selector(fmt),
    })
    if fmt == "mp3":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192",
        }]
    else:
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True) or {}


async def _download_ytdlp(vid: str, fmt: str, folder: str) -> YTFile:
    try:
        info = await asyncio.to_thread(_download_sync, vid, fmt, folder)
    except Exception as e:
        log.warning("yt-dlp download failed: %s", str(e)[:300])
        raise YTError(_friendly(str(e)))

    files = [
        os.path.join(folder, n) for n in os.listdir(folder)
        if os.path.splitext(n)[1].lower() in (".mp4", ".mkv", ".webm", ".mp3", ".m4a")
    ]
    if not files:
        raise YTError(f"Video {BIG_MAX_MB}MB se badi hai ya download nahi hui. 360p ya MP3 try karo.")
    path = max(files, key=os.path.getsize)
    if os.path.getsize(path) > BIG_MAX_MB * 1024 * 1024:
        raise YTError(f"Video {BIG_MAX_MB}MB se badi hai. 360p ya MP3 try karo.")

    return YTFile(
        path=path,
        kind="audio" if fmt == "mp3" else "video",
        title=info.get("title") or "YouTube",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=int(info.get("duration") or 0),
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
    )


async def _probe_media(path: str):
    """(duration_sec, width, height) via ffmpeg, so Telegram shows the video correctly."""
    try:
        from pinterest import _ffmpeg_exe
        exe = _ffmpeg_exe()
        proc = await asyncio.create_subprocess_exec(
            exe, "-hide_banner", "-i", path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except Exception:
        return 0, 0, 0
    text = err.decode(errors="ignore")
    dur = 0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        dur = int(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
    w = h = 0
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    return dur, w, h


def _rm(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------- API download ----------
def _find_link(obj):
    """JSON response me se pehla download link nikalo."""
    if isinstance(obj, str):
        return obj if obj.startswith(("http://", "https://", "/")) else None
    if isinstance(obj, dict):
        for k in ("download_url", "downloadUrl", "link", "url", "file", "stream", "result", "data"):
            if k in obj:
                found = _find_link(obj[k])
                if found:
                    return found
        for v in obj.values():
            found = _find_link(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find_link(v)
            if found:
                return found
    return None


async def _stream_to_file(c: httpx.AsyncClient, url: str, path: str, params=None):
    """Returns (size, status, ctype, body). size > 0 matlab file mil gayi."""
    limit = BIG_MAX_MB * 1024 * 1024
    size = 0
    async with c.stream("GET", url, params=params) as r:
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code != 200 or "json" in ctype or ctype.startswith("text/"):
            return 0, r.status_code, ctype, (await r.aread())[:2000]
        with open(path, "wb") as f:
            async for chunk in r.aiter_bytes(262144):
                size += len(chunk)
                if size > limit:
                    raise YTError(f"Video {BIG_MAX_MB}MB se badi hai. MP3 ya chhota video try karo.")
                f.write(chunk)
    return size, 200, ctype, b""


async def _finish_api_file(path: str, vid: str, fmt: str) -> YTFile:
    """ffmpeg probe aur title lookup ek saath (tez)."""
    (dur, w, h), meta = await asyncio.gather(
        _probe_media(path),
        _api_info(vid),
    )
    if not meta:
        meta = await _oembed_info(vid)
    return YTFile(
        path=path, kind="audio" if fmt == "mp3" else "video",
        title=(meta.title if meta else "YouTube"),
        uploader=(meta.uploader if meta else ""),
        duration=dur or (meta.duration if meta else 0),
        width=w, height=h,
    )


async def _download_api(vid: str, fmt: str, folder: str):
    """Path 1: /download?query=<id>&dl_type=audio|video&quality=360|720. None = didn't work."""
    if not API_KEY:
        log.warning("API_KEY set nahi hai (Heroku Config Vars check karo)")
    is_audio = fmt == "mp3"
    path = os.path.join(folder, f"{vid}.{'mp3' if is_audio else 'mp4'}")
    params = _api_params(query=vid, dl_type="audio" if is_audio else "video")
    if not is_audio:
        params["quality"] = FORMATS[fmt]

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0),
                                 follow_redirects=True) as c:
        for attempt in (1, 2):
            try:
                size, status, ctype, body = await _stream_to_file(
                    c, f"{API_URL}/download", path, params
                )
                if not size:
                    text = body.decode("utf-8", "replace")
                    log.warning("API /download attempt %d -> HTTP %s %s %s",
                                attempt, status, ctype, text[:300])
                    if status in (401, 403):
                        log.warning("API key invalid/expire lag rahi hai. Nayi key lo.")
                        return None
                    link = None
                    try:
                        link = _find_link(json.loads(text))
                    except Exception:
                        pass
                    if link:
                        link = _abs_api_url(link)
                        log.info("API JSON link mila: %s", link[:120])
                        size, status, ctype, body = await _stream_to_file(c, link, path)
                if size >= 10_000:
                    return await _finish_api_file(path, vid, fmt)
                if size:
                    log.warning("API /download sirf %d bytes mile", size)
                _rm(path)
            except YTError:
                _rm(path)
                raise
            except Exception as e:
                log.warning("API /download attempt %d failed: %r", attempt, e)
                _rm(path)
            await asyncio.sleep(2)
    return None


def _pick_format(data: dict, fmt: str):
    """extract response ke `formats` me se sahi quality ka URL chuno."""
    is_audio = fmt == "mp3"
    limit_h = FORMATS[fmt]
    best = None
    for f in data.get("formats") or []:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        typ = str(f.get("type") or "").lower()
        if is_audio:
            if typ == "audio":
                return f["url"]
            continue
        if typ != "video" or f.get("has_audio") is False:
            continue
        m = re.search(r"\d+", str(f.get("quality") or ""))
        q = int(m.group()) if m else 0
        if q and q <= limit_h and (best is None or q > best[0]):
            best = (q, f["url"])
    if best:
        return best[1]
    return None if is_audio else data.get("download_url")


async def _download_extract(vid: str, fmt: str, folder: str):
    """Path 2: /api/platform/extract se stream URL, phir file. None = didn't work."""
    is_audio = fmt == "mp3"
    path = os.path.join(folder, f"{vid}.{'mp3' if is_audio else 'mp4'}")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0),
                                     follow_redirects=True) as c:
            r = await c.get(
                f"{API_URL}/api/platform/extract",
                params=_api_params(url=f"https://www.youtube.com/watch?v={vid}"),
                timeout=60,
            )
            if r.status_code != 200:
                log.warning("API extract -> HTTP %s %s", r.status_code, r.text[:200])
                return None
            url = _pick_format(r.json(), fmt)
            if not url:
                log.warning("API extract: %s ke liye format nahi mila", fmt)
                return None
            size, status, ctype, body = await _stream_to_file(c, _abs_api_url(url), path)
            if size < 10_000:
                log.warning("API extract file fail -> HTTP %s %s %r", status, ctype, body[:200])
                _rm(path)
                return None
        return await _finish_api_file(path, vid, fmt)
    except YTError:
        _rm(path)
        raise
    except Exception as e:
        log.warning("API extract failed: %r", e)
        _rm(path)
        return None


async def download(vid: str, fmt: str, folder: str) -> YTFile:
    if fmt not in FORMATS:
        raise YTError("Invalid format.")
    if API_URL:
        for attempt_fn in (_download_api, _download_extract):
            f = await attempt_fn(vid, fmt, folder)
            if f:
                return f
        log.info("API did not deliver, trying yt-dlp")
    return await _download_ytdlp(vid, fmt, folder)


async def split_file(path: str, folder: str, duration: int = 0,
                     part_mb: int = 47, limit: int = None) -> list:
    """Cut a big file into playable parts that each fit Telegram's bot upload limit.
    Uses stream copy (no re-encoding), so it is fast and lossless."""
    from pinterest import _ffmpeg_exe

    limit = limit or (MAX_FILE_MB * 1024 * 1024 - 1024 * 1024)
    size = os.path.getsize(path)
    ext = os.path.splitext(path)[1].lower() or ".mp4"
    est_parts = max(2, math.ceil(size / (part_mb * 1024 * 1024)))
    dur = duration or 600
    factor = 0.85

    for attempt in range(4):
        seg = max(5, int(dur / est_parts * factor))
        out_dir = os.path.join(folder, f"parts{attempt}")
        os.makedirs(out_dir, exist_ok=True)
        cmd = [
            _ffmpeg_exe(), "-y", "-loglevel", "error", "-i", path,
            "-map", "0", "-c", "copy", "-f", "segment",
            "-segment_time", str(seg), "-reset_timestamps", "1",
        ]
        if ext == ".mp4":
            cmd += ["-segment_format_options", "movflags=+faststart"]
        cmd.append(os.path.join(out_dir, f"part_%03d{ext}"))

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        parts = sorted(os.path.join(out_dir, n) for n in os.listdir(out_dir))
        if proc.returncode == 0 and parts and all(os.path.getsize(p) <= limit for p in parts):
            return parts
        log.warning("split attempt %d: rc=%s parts=%d %s", attempt, proc.returncode, len(parts),
                    err.decode(errors="ignore")[:200])
        factor *= 0.7  # parts came out too big -> cut shorter pieces
    raise YTError("Badi file parts me split nahi ho payi. 360p ya MP3 try karo.")
