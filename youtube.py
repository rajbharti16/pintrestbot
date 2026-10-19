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
import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from config import MAX_FILE_MB, eid, em
from tg import btn

log = logging.getLogger("pinbot")

YT_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]*?v=|shorts/|live/)|youtu\.be/)[\w-]{11}[^\s]*",
    re.I,
)
ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})")
MAX_MIN = int(os.getenv("YT_MAX_MIN", "60"))

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
def _to_text(data: bytes):
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _cookie_file():
    """YT_COOKIES_B64 -> cookies.txt. Tolerates plain text / UTF-16 files and
    ignores a broken value instead of crashing yt-dlp."""
    raw = os.getenv("YT_COOKIES_B64", "").strip()
    if not raw:
        return None

    try:
        data = base64.b64decode(re.sub(r"\s+", "", raw), validate=True)
    except Exception:
        data = raw.replace("\\n", "\n").replace("\\t", "\t").encode()  # plain text pasted

    text = _to_text(data)
    if text is None or ("youtube.com" not in text and "google.com" not in text):
        log.warning("YT_COOKIES_B64 galat hai (valid base64 of cookies.txt nahi), ignore kar raha hu")
        return None

    path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.replace("\r\n", "\n"))
    return path


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
        return "Cookies file kharab hai, owner YT_COOKIES_B64 dobara banaye."
    if "not a bot" in low or "sign in to confirm" in low:
        return "YouTube ne bot detect kar liya. Bot owner ko YT_COOKIES_B64 set karna padega."
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
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
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
        raise YTError(f"File {MAX_FILE_MB}MB se badi hai ya download nahi hui. Kam quality try karo.")
    path = max(files, key=os.path.getsize)
    if os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
        raise YTError(f"File {MAX_FILE_MB}MB se badi hai. Kam quality try karo.")

    return YTFile(
        path=path,
        kind="audio" if fmt == "mp3" else "video",
        title=info.get("title") or "YouTube",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=int(info.get("duration") or 0),
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
    )
  
