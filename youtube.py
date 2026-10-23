"""YouTube downloader: video (360p / 720p) and MP3, via yt-dlp.

Flow: user sends a link -> bot shows title + thumbnail with quality buttons ->
user taps a button -> bot downloads and sends the file.

Optional env:
  YT_COOKIES_B64  base64 of a cookies.txt (YouTube blocks most server IPs without it)
  YT_PROXY        proxy URL, e.g. http://user:pass@host:port
  YT_MAX_MIN      max video length in minutes (default 60)
"""
import asyncio
import base64
import html
import logging
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from config import MAX_FILE_MB, MONGO_DB, MONGO_URI, eid, em
from tg import btn

log = logging.getLogger("pinbot")

YT_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]*?v=|shorts/|live/)|youtu\.be/)[\w-]{11}[^\s]*",
    re.I,
)
ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})")
MAX_MIN = int(os.getenv("YT_MAX_MIN", "60"))
# Files above 50MB are downloaded (up to this size) and sent in parts
BIG_MAX_MB = int(os.getenv("YT_BIG_MAX_MB", "300"))

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
    return (
        f"{em('pin', '📌')} <b>{html.escape(info.title[:200])}</b>\n"
        f"{em('link', '🔗')} {html.escape(info.uploader[:60])}  •  {_fmt_duration(info.duration)}\n\n"
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


# ---------- yt-dlp plumbing ----------
ENV_COOKIE_PATH = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
OWNER_COOKIE_PATH = os.path.join(tempfile.gettempdir(), "yt_cookies_owner.txt")


def _to_text(data: bytes):
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def normalize_cookies(text: str):
    """Repair a cookies.txt into strict Netscape format. Returns (text, count) or (None, 0).
    Fixes: spaces instead of tabs, CRLF, extra comment lines, missing header."""
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
    if n and any("youtube.com" in l or "google.com" in l for l in lines):
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
        log.warning("YT_COOKIES_B64 galat hai, ignore kar raha hu. Bot ko /ytcookies se cookies.txt bhejo.")
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
        return "YouTube ne bot detect kar liya. Bot owner ko /ytcookies se cookies.txt bhejni padegi."
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


async def get_info(url: str) -> YTInfo:
    try:
        info = await asyncio.to_thread(_info_sync, url)
    except Exception as e:
        log.warning("yt-dlp info failed: %s", str(e)[:400])
        raise YTError(_friendly(str(e)))
    if not info:
        raise YTError("Video ki info nahi mili.")
    if info.get("is_live"):
        raise YTError("Live stream download nahi ho sakti.")
    dur = int(info.get("duration") or 0)
    if dur > MAX_MIN * 60:
        raise YTError(f"Video {MAX_MIN} minute se lamba hai.")
    return YTInfo(
        id=info["id"],
        title=info.get("title") or "YouTube video",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=dur,
        thumbnail=info.get("thumbnail") or "",
    )


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


async def download(vid: str, fmt: str, folder: str) -> YTFile:
    if fmt not in FORMATS:
        raise YTError("Invalid format.")
    try:
        info = await asyncio.to_thread(_download_sync, vid, fmt, folder)
    except Exception as e:
        log.warning("yt-dlp download failed: %s", str(e)[:400])
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
  
