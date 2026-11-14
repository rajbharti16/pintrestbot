"""YouTube downloader: video and MP3 (sirf API se, cookies / yt-dlp nahi).

Download order:
  1) {API_URL}/download                (direct file, quality param)
  2) {API_URL}/api/platform/extract    (stream URL)

Flow: user sends a link -> bot shows title + thumbnail with quality buttons ->
user taps a button -> bot downloads and sends the file.

Config Vars (Heroku -> Settings):
  API_KEY   tumhari API key (code me hard-code mat karo)
  API_URL   default https://www.riteshyt.in
Optional:
  YT_MAX_MIN (default 60), YT_BIG_MAX_MB
"""
import asyncio
import html
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx

from config import API_HASH, API_ID, MAX_FILE_MB, eid, em
from tg import btn

log = logging.getLogger("pinbot")

YT_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]*?v=|shorts/|live/)|youtu\.be/)[\w-]{11}[^\s]*",
    re.I,
)
ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})")
MAX_MIN = int(os.getenv("YT_MAX_MIN", "60"))
API_URL = os.getenv("API_URL", "https://www.riteshyt.in").strip().rstrip("/")
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


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


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
    if not vid:
        raise YTError("Valid YouTube link nahi mila.")

    # 1) API /details  2) public oEmbed  3) minimal info (download API se hi hoga)
    info = await _api_info(vid)
    if info:
        return _check_limits(info)
    info = await _oembed_info(vid)
    if info:
        return info
    return YTInfo(id=vid, title="YouTube video", uploader="", duration=0, thumbnail=_thumb(vid))


# ---------- ffmpeg helpers ----------
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


async def split_file(path: str, folder: str, duration: int):
    """Badi file ko Telegram limit ke andar parts me todo (re-encode nahi hota)."""
    from pinterest import _ffmpeg_exe

    limit = MAX_FILE_MB * 1024 * 1024
    size = os.path.getsize(path)
    if not duration:
        duration, _, _ = await _probe_media(path)
    if not duration:
        raise YTError("File badi hai aur parts me nahi tod paya. Chhota video ya MP3 try karo.")

    ext = os.path.splitext(path)[1] or ".mp4"
    exe = _ffmpeg_exe()
    n = max(2, math.ceil(size / (limit * 0.9)))
    for _ in range(3):
        for name in os.listdir(folder):
            if name.startswith("part_"):
                _rm(os.path.join(folder, name))
        seg = max(1, math.ceil(duration / n))
        proc = await asyncio.create_subprocess_exec(
            exe, "-y", "-loglevel", "error", "-i", path, "-c", "copy",
            "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
            os.path.join(folder, f"part_%03d{ext}"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        parts = sorted(
            os.path.join(folder, x) for x in os.listdir(folder) if x.startswith("part_")
        )
        if proc.returncode == 0 and parts and all(os.path.getsize(p) <= limit for p in parts):
            return parts
        log.warning("split retry (n=%d): rc=%s %s", n, proc.returncode,
                    err.decode(errors="ignore")[:200])
        n *= 2
    raise YTError("File parts me nahi tut payi. Chhota video ya MP3 try karo.")


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
    best, best_score = None, None
    for f in data.get("formats") or []:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        typ = str(f.get("type") or f.get("mime_type") or f.get("mimeType") or "").lower()
        vcodec = str(f.get("vcodec") or "").lower()
        acodec = str(f.get("acodec") or "").lower()
        height = int(_num(f.get("height")))
        has_video = height > 0 or typ.startswith("video") or vcodec not in ("", "none")
        audio_only = (
            (typ.startswith("audio") or vcodec == "none" or "audio only" in typ)
            and not has_video
        )
        if is_audio:
            if not audio_only:
                continue
            score = (_num(f.get("abr") or f.get("bitrate") or f.get("tbr")),)
        else:
            if not has_video or (height and height > limit_h):
                continue
            video_only = acodec == "none" or "video only" in typ or f.get("has_audio") is False
            # audio wali format pehle, phir sabse badi height jo limit ke andar ho
            score = (0 if video_only else 1, height, _num(f.get("tbr") or f.get("bitrate")))
        if best_score is None or score > best_score:
            best, best_score = f["url"], score
    return best


async def _download_extract(vid: str, fmt: str, folder: str):
    """Path 2: /api/platform/extract se stream URL lo, phir download karo."""
    link = f"https://www.youtube.com/watch?v={vid}"
    is_audio = fmt == "mp3"
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0),
                                 follow_redirects=True) as c:
        data = None
        for key in ("url", "link"):
            try:
                r = await c.get(
                    f"{API_URL}/api/platform/extract", params=_api_params(**{key: link})
                )
                if r.status_code in (401, 403):
                    log.warning("API extract -> HTTP %s (key check karo)", r.status_code)
                    return None
                if r.status_code == 200:
                    d = r.json()
                    if isinstance(d, dict):
                        data = d
                        break
                log.warning("API extract (%s=) -> HTTP %s %s", key, r.status_code, r.text[:200])
            except Exception as e:
                log.warning("API extract (%s=) failed: %r", key, e)
        if not data:
            return None

        stream = _pick_format(data, fmt) or _find_link(data)
        if not stream:
            log.warning("API extract: koi stream URL nahi mila")
            return None
        stream = _abs_api_url(stream)

        path = os.path.join(folder, f"{vid}.{'m4a' if is_audio else 'mp4'}")
        try:
            size, status, ctype, _ = await _stream_to_file(c, stream, path)
        except YTError:
            _rm(path)
            raise
        except Exception as e:
            log.warning("API extract stream failed: %r", e)
            _rm(path)
            return None
        if size >= 10_000:
            return await _finish_api_file(path, vid, fmt)
        log.warning("API extract stream -> HTTP %s %s, %d bytes", status, ctype, size)
        _rm(path)
    return None


async def download(vid: str, fmt: str, folder: str) -> YTFile:
    if fmt not in FORMATS:
        raise YTError("Ye quality available nahi hai.")
    for method in (_download_api, _download_extract):
        f = await method(vid, fmt, folder)
        if f:
            return f
    raise YTError("Download abhi nahi ho paya. Thodi der baad try karo.")
