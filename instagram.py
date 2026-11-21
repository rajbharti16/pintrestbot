"""
Instagram downloader for the Telegram bot.

Flow:
  - Public reel / post / tv  -> yt-dlp first (video + audio merge), API fallback
  - Stories / highlights     -> directly API (yt-dlp needs login for these)
  - Photo posts              -> API; agar song/audio mile to photo + song ka mp4 banta hai
  - Video bina audio ka mile -> API ka audio (agar hai) ffmpeg se mux hota hai

API: api.riteshyt.in  (API_URL + API_KEY environment variables)
No Instagram login session or cookies required.

IMPORTANT: Audio merge ke liye server par ffmpeg (+ffprobe) hona chahiye.
  Heroku : heroku buildpacks:add --index 1 https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest.git
  VPS    : sudo apt install -y ffmpeg
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import httpx

log = logging.getLogger("pinbot.instagram")

# Config Vars: API_URL, API_KEY. Key ko code me hard-code mat karo.
API_URL = os.getenv("API_URL", "https://api.riteshyt.in").strip().rstrip("/")
API_KEY = os.getenv("API_KEY", "").strip()

MAX_STORY_ITEMS = 10
PHOTO_VIDEO_MAX_SECONDS = 30  # photo + song video ki max length

IG_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/"
    r"(?:[\w.]+/)?"
    r"(?:reel|reels|p|tv|stories|s|share)/[^\s<>\]\[\"']+",
    re.IGNORECASE,
)

# Stories / highlights -> API only
API_ONLY_RE = re.compile(r"instagram\.com/(?:stories/|s/)", re.IGNORECASE)

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


class IGError(Exception):
    """Instagram download or API error."""


class IGSessionError(IGError):
    """Legacy compatibility only; this module does not use Instagram sessions."""


@dataclass
class IGItem:
    path: str
    kind: str  # "video" or "photo"


@dataclass
class IGResult:
    url: str
    title: str
    total: int
    items: list[IGItem]


def set_dead_hook(_hook):
    """Legacy compatibility: no session-expiry hook is used."""
    return None


async def watchdog_once():
    """Legacy compatibility: session watchdog removed; intentionally does nothing."""
    return None


def _clean_url(url: str) -> str:
    m = IG_RE.search((url or "").strip())
    if not m:
        raise IGError("Valid Instagram Reel/Post/Story link bhejo.")
    return m.group(0).rstrip(".,!?);]")


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


async def _run_cmd(*cmd: str, timeout: int = 180) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise IGError("ffmpeg timeout ho gaya.")
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
        raise IGError(f"ffmpeg fail: {tail[0][:200]}")
    return out.decode("utf-8", "replace")


async def _has_audio(path: str):
    """True / False if known, None if ffprobe unavailable or failed."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = await _run_cmd(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", path,
            timeout=30,
        )
        return "audio" in out
    except Exception:
        return None


async def _mux_audio(video_path: str, audio_path: str, out_path: str) -> None:
    await _run_cmd(
        "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart", out_path,
    )


async def _photo_to_video(img_path: str, audio_path: str, out_path: str) -> None:
    await _run_cmd(
        "ffmpeg", "-y", "-loop", "1", "-framerate", "2", "-i", img_path, "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=w=trunc(min(1080\\,iw)/2)*2:h=-2,format=yuv420p",
        "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast", "-r", "15",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-t", str(PHOTO_VIDEO_MAX_SECONDS),
        "-movflags", "+faststart", out_path,
    )


async def _result_is_silent(result: IGResult) -> bool:
    for item in result.items:
        if item.kind == "video" and (await _has_audio(item.path)) is False:
            return True
    return False


# ---------------------------------------------------------------------------
# 1) yt-dlp (primary for public reels / posts)
# ---------------------------------------------------------------------------
def _yt_dlp_worker(link: str, folder: str, all_slides: bool) -> IGResult:
    """Sync yt-dlp worker; runs in a thread so the bot event loop stays free."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise IGError("yt-dlp install nahi hai (pip install -U yt-dlp).") from exc

    if _has_ffmpeg():
        # video + audio alag aate hain -> ffmpeg se merge (isi se sound aati hai)
        fmt = "bestvideo*+bestaudio/best"
    else:
        # ffmpeg nahi hai: sirf wahi format lo jisme video aur audio dono ho
        fmt = "best[vcodec!=none][acodec!=none]/best"

    opts = {
        "outtmpl": os.path.join(folder, "%(id)s_%(autonumber)03d.%(ext)s"),
        "format": fmt,
        "format_sort": ["vcodec:h264", "res", "acodec:aac"],
        "merge_output_format": "mp4",
        "noplaylist": not all_slides,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "socket_timeout": 15,
        "restrictfilenames": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(link, download=True)
            if not info:
                raise IGError("yt-dlp ko media nahi mili.")
            entries = info.get("entries")
            leaf_infos = [x for x in entries if x] if isinstance(entries, list) else [info]
            if not all_slides and leaf_infos:
                leaf_infos = leaf_infos[:1]

            files, seen = [], set()
            for entry in leaf_infos:
                candidates = []
                for req in entry.get("requested_downloads") or []:
                    if req.get("filepath"):
                        candidates.append(req["filepath"])
                if entry.get("_filename"):
                    candidates.append(entry["_filename"])
                try:
                    candidates.append(ydl.prepare_filename(entry))
                except Exception:
                    pass
                for cand in candidates:
                    cand = os.path.abspath(cand)
                    if cand not in seen and os.path.isfile(cand) and os.path.getsize(cand) > 0:
                        seen.add(cand)
                        files.append(cand)
                        break

            if not files:
                for p in sorted(Path(folder).iterdir()):
                    if p.is_file() and p.stat().st_size > 0 and p.suffix.lower() not in (".json", ".part"):
                        files.append(str(p))
            if not files:
                raise IGError("yt-dlp ne download kiya, lekin file locate nahi hui.")

            result = [
                IGItem(path=p, kind="photo" if Path(p).suffix.lower() in IMG_EXT else "video")
                for p in files
            ]
            title = str(info.get("title") or info.get("description") or "Instagram media")
            total = int(info.get("playlist_count") or info.get("n_entries") or len(result) or 1)
            return IGResult(url=link, title=title, total=max(total, len(result)), items=result)
    except IGError:
        raise
    except Exception as exc:
        raise IGError(f"yt-dlp fail: {str(exc).replace(chr(10), ' ')[:300]}") from exc


# ---------------------------------------------------------------------------
# 2) API (fallback + stories / highlights + photo posts)
# ---------------------------------------------------------------------------
def _walk_dicts(obj):
    """Yield every dict nested in a JSON response (parents before children)."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            if isinstance(value, (dict, list)):
                yield from _walk_dicts(value)


def _first_value(objects, keys):
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _normalize_media_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return urljoin(API_URL + "/", value.lstrip("/"))
    if value.startswith(("https://", "http://")):
        return value
    return None


def _looks_media(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(x in path for x in (".mp4", ".m4v", ".jpg", ".jpeg", ".png", ".webp", "/stream", "/download"))


_LIST_KEYS = ("items", "media", "carousel_media", "media_urls", "resources", "results", "stories", "medias")
_DIRECT_KEYS = (
    "download_url", "video_url", "videoUrl", "image_url", "imageUrl",
    "display_url", "displayUrl", "media_url", "mediaUrl", "direct_url", "src",
)
_AUDIO_KEYS = (
    "audio_url", "audioUrl", "music_url", "musicUrl", "song_url", "songUrl",
    "original_audio_url", "audio_download_url", "music_download_url", "audio_src",
    "audio_stream_url",
)
_AUDIO_CONTAINER_KEYS = ("music", "audio", "song", "sound", "music_info", "audio_info")


def _entry_url(entry, allow_plain_url: bool):
    """(url, kind_hint) from a dict/str entry."""
    if isinstance(entry, str):
        return _normalize_media_url(entry), ""
    if not isinstance(entry, dict):
        return None, ""
    kind = str(entry.get("type") or entry.get("media_type") or entry.get("kind") or "").lower()
    for key in _DIRECT_KEYS:
        url = _normalize_media_url(entry.get(key))
        if url:
            return url, kind
    if allow_plain_url:
        url = _normalize_media_url(entry.get("url"))
        if url and _looks_media(url):
            return url, kind
    return None, ""


def _quality_num(value) -> int:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else 0


def _media_urls(payload) -> list:
    """Returns [(url, kind_hint), ...]. Single reel -> exactly one item."""
    objects = list(_walk_dicts(payload))

    # 1) carousel / stories lists
    for obj in objects:
        for key in _LIST_KEYS:
            value = obj.get(key)
            if isinstance(value, list) and value:
                out, seen = [], set()
                for entry in value:
                    url, kind = _entry_url(entry, allow_plain_url=True)
                    if kind == "audio":
                        continue
                    if url and url not in seen:
                        seen.add(url)
                        out.append((url, kind))
                if out:
                    return out

    # 2) single media: download_url / video_url / image_url ...
    for obj in objects:
        url, kind = _entry_url(obj, allow_plain_url=False)
        if url and kind != "audio":
            return [(url, kind)]

    # 3) formats list: best video that has audio
    for obj in objects:
        formats = obj.get("formats")
        if isinstance(formats, list):
            best = None
            for f in formats:
                if not isinstance(f, dict) or str(f.get("type") or "video").lower() != "video":
                    continue
                if f.get("has_audio") is False:
                    continue
                url = _normalize_media_url(f.get("url"))
                if url and (best is None or _quality_num(f.get("quality")) > best[0]):
                    best = (_quality_num(f.get("quality")), url)
            if best:
                return [(best[1], "video")]

    # 4) last resort: plain "url" that looks like media
    for obj in objects:
        url, kind = _entry_url(obj, allow_plain_url=True)
        if url and kind != "audio":
            return [(url, kind)]
    return []


def _find_audio_url(payload):
    """API response me song / original audio ka URL dhundo (agar API deti hai)."""
    objects = list(_walk_dicts(payload))

    for obj in objects:
        for key in _AUDIO_KEYS:
            url = _normalize_media_url(obj.get(key))
            if url:
                return url

    for obj in objects:
        for key in _AUDIO_CONTAINER_KEYS:
            value = obj.get(key)
            if isinstance(value, str):
                url = _normalize_media_url(value)
                if url:
                    return url
            elif isinstance(value, dict):
                for sub in ("url", "download_url", "audio_url", "src", "stream_url", "progressive_download_url"):
                    url = _normalize_media_url(value.get(sub))
                    if url:
                        return url

    for obj in objects:
        kind = str(obj.get("type") or obj.get("media_type") or obj.get("kind") or "").lower()
        if kind == "audio":
            for sub in ("url", "download_url", "src"):
                url = _normalize_media_url(obj.get(sub))
                if url:
                    return url
    return None


def _add_api_key_to_stream(url: str) -> str:
    """API ke apne stream routes ko bhi key chahiye ho sakti hai."""
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != urlparse(API_URL).netloc:
        return url
    query = parse_qs(parsed.query, keep_blank_values=True)
    if API_KEY and not query.get("api_key"):
        query["api_key"] = [API_KEY]
        parsed = parsed._replace(query=urlencode(query, doseq=True))
    return urlunparse(parsed)


def _suffix_from_url(url: str, content_type: str = "") -> str:
    path = urlparse(url).path.lower()
    for ext in (".mp4", ".m4v", ".mov", ".webm", ".mp3", ".m4a", ".aac") + IMG_EXT + (".gif",):
        if path.endswith(ext):
            return ext
    ctype = (content_type or "").split(";", 1)[0].lower()
    return {
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/x-m4a": ".m4a",
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    }.get(ctype, "")


async def _download_url(client: httpx.AsyncClient, url: str, folder: str, index, hint: str) -> IGItem:
    url = _add_api_key_to_stream(url)
    is_photo_hint = hint in ("image", "photo", "picture")
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=90) as response:
            response.raise_for_status()
            ctype = response.headers.get("content-type", "")
            low = ctype.lower()
            if "json" in low or "text/html" in low:
                body = (await response.aread())[:300].decode("utf-8", "replace")
                raise IGError(f"API ne file ke bajaye response diya: {body[:200]}")
            suffix = _suffix_from_url(url, ctype)
            if not suffix:
                suffix = ".jpg" if is_photo_hint else ".mp4"
            elif is_photo_hint and suffix not in IMG_EXT:
                suffix = ".jpg"
            path = os.path.join(folder, f"instagram_{index}{suffix}")
            with open(path, "wb") as out:
                async for chunk in response.aiter_bytes(262144):
                    out.write(chunk)
        if os.path.getsize(path) < 128:
            raise IGError("Empty ya invalid media file mili.")
        kind = "photo" if suffix in IMG_EXT else "video"
        if hint in ("video", "reel", "animation") and suffix not in IMG_EXT:
            kind = "video"
        return IGItem(path=path, kind=kind)
    except httpx.HTTPStatusError as exc:
        raise IGError(f"Media download HTTP {exc.response.status_code}.") from exc
    except httpx.HTTPError as exc:
        raise IGError(f"Media download network error: {exc}") from exc


async def _attach_audio(client: httpx.AsyncClient, items: list, audio_url, folder: str) -> list:
    """
    - Photo  -> photo + song ka mp4 (kind="video")
    - Video bina audio ka -> audio mux
    Koi bhi step fail ho to original item hi return hota hai (error nahi aata).
    """
    has_photo = any(i.kind == "photo" for i in items)

    if not audio_url:
        if has_photo:
            log.warning("Photo post me API ne audio/song URL nahi diya; photo as-is bheji ja rahi hai.")
        return items

    if not _has_ffmpeg():
        log.warning("ffmpeg install nahi hai, audio attach skip. (apt install ffmpeg / Heroku ffmpeg buildpack)")
        return items

    need = has_photo
    if not need:
        for item in items:
            if item.kind == "video" and (await _has_audio(item.path)) is False:
                need = True
                break
    if not need:
        return items

    try:
        audio = await _download_url(client, audio_url, folder, "audio", "audio")
    except Exception as exc:
        log.warning("Audio download fail: %s", str(exc)[:200])
        return items

    out = []
    for n, item in enumerate(items, 1):
        try:
            if item.kind == "photo":
                dst = os.path.join(folder, f"instagram_{n}_song.mp4")
                await _photo_to_video(item.path, audio.path, dst)
                if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
                    out.append(IGItem(path=dst, kind="video"))
                    continue
            elif item.kind == "video" and (await _has_audio(item.path)) is False:
                dst = os.path.join(folder, f"instagram_{n}_mux.mp4")
                await _mux_audio(item.path, audio.path, dst)
                if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
                    out.append(IGItem(path=dst, kind="video"))
                    continue
        except Exception as exc:
            log.warning("Audio attach fail (item %s): %s", n, str(exc)[:200])
        out.append(item)
    return out


async def _api_download(link: str, folder: str, all_slides: bool) -> IGResult:
    os.makedirs(folder, exist_ok=True)
    params = {"url": link}
    if API_KEY:
        params["api_key"] = API_KEY
    else:
        log.warning("API_KEY set nahi hai (Heroku Config Vars check karo)")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MediaDownloader/1.0)"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0),
                                 headers=headers, follow_redirects=True) as client:
        try:
            response = await client.get(f"{API_URL}/api/platform/extract", params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            log.warning("API extract HTTP %s: %s", code, exc.response.text[:200])
            if code in (401, 403):
                raise IGError("API key invalid/expire hai (free key 30 din chalti hai), nayi key lo.") from exc
            if code == 429:
                raise IGError("API rate limit (429); thodi der baad try karo.") from exc
            raise IGError(f"API HTTP {code}.") from exc
        except httpx.HTTPError as exc:
            raise IGError(f"API connect nahi hua: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise IGError("API ne valid JSON nahi diya.") from exc

        objects = list(_walk_dicts(payload))
        if _first_value(objects, ("success", "ok")) is False:
            message = _first_value(objects, ("message", "detail", "error", "reason"))
            raise IGError(f"API: {str(message or 'request failed')[:250]}")

        title = str(_first_value(objects, ("title", "caption", "description", "shortcode")) or "Instagram media")
        canonical = str(_first_value(objects, ("permalink", "canonical_url", "post_url")) or link)

        media = _media_urls(payload)
        if not media:
            message = _first_value(objects, ("message", "detail", "error", "reason"))
            raise IGError(f"API response me media URL nahi mila. {str(message or '')[:150]}")

        audio_url = _find_audio_url(payload)
        log.info("API media=%d audio=%s", len(media), "yes" if audio_url else "no")

        declared = _first_value(objects, ("total", "media_count", "carousel_count"))
        try:
            total = max(int(declared), len(media))
        except (TypeError, ValueError):
            total = len(media)

        if all_slides:
            media = media[:MAX_STORY_ITEMS]
        elif len(media) > 1:
            media = media[:1]  # bot "All photos" button se baaki bhejta hai

        results = await asyncio.gather(
            *(_download_url(client, url, folder, i, hint) for i, (url, hint) in enumerate(media, 1)),
            return_exceptions=True,
        )
        items = [r for r in results if isinstance(r, IGItem)]
        if not items:
            first_err = next((r for r in results if isinstance(r, Exception)), None)
            raise IGError(str(first_err) if first_err else "API se koi media download nahi hui.")

        items = await _attach_audio(client, items, audio_url, folder)
        return IGResult(url=canonical, title=title, total=max(total, len(items)), items=items)


# ---------------------------------------------------------------------------
# Public entry point (same signature the bot already uses)
# ---------------------------------------------------------------------------
def _reset_folder(folder: str):
    """Failed attempt ki adhoori files hata do."""
    for p in Path(folder).iterdir():
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


async def fetch_instagram(url: str, folder: str, all_slides: bool = False) -> IGResult:
    link = _clean_url(url)
    os.makedirs(folder, exist_ok=True)

    # Stories / highlights: yt-dlp bina login ke nahi kar sakta -> seedha API
    if API_ONLY_RE.search(link):
        return await _api_download(link, folder, True)

    # Public reel / post: yt-dlp pehle
    ytdlp_result = None
    ytdlp_error = None
    try:
        ytdlp_result = await asyncio.to_thread(_yt_dlp_worker, link, folder, all_slides)
    except Exception as exc:
        ytdlp_error = exc
        log.warning("yt-dlp failed, trying API (%s)", str(exc)[:200])

    if ytdlp_result is not None:
        if not await _result_is_silent(ytdlp_result):
            return ytdlp_result
        # yt-dlp ne bina sound ki video di -> API se audio wala version try karo
        log.warning("yt-dlp video me audio nahi mila, API try kar raha hoon")
        api_folder = os.path.join(folder, "_api")
        try:
            api_result = await _api_download(link, api_folder, all_slides)
            if not await _result_is_silent(api_result):
                return api_result
        except Exception as exc:
            log.warning("API audio retry failed: %s", str(exc)[:200])
        return ytdlp_result  # kam se kam video to mile

    _reset_folder(folder)
    try:
        return await _api_download(link, folder, all_slides)
    except Exception as api_error:
        raise IGError(
            f"yt-dlp: {str(ytdlp_error)[:120]} | API: {str(api_error)[:200]}"
        ) from api_error
