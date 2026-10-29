"""
Instagram downloader for the Telegram bot.

Flow:
  - Public reel / post / tv  -> yt-dlp first (free), API fallback if it fails
  - Stories / highlights     -> directly API (yt-dlp needs login for these)

API: RiteshYT (API_URL + API_KEY environment variables)
No Instagram login session or cookies required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import httpx

log = logging.getLogger("pinbot.instagram")

# Set these as environment variables. Do not hard-code your API key.
API_URL = os.getenv("API_URL", "https://api.riteshyt.in").rstrip("/")
API_KEY = os.getenv("API_KEY", "riteshfreee0aac51ec0bc9720157ffc57").strip()

IG_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/"
    r"(?:[\w.]+/)?"
    r"(?:reel|reels|p|tv|stories|s|share)/[^\s<>\]\[\"']+",
    re.IGNORECASE,
)

# Stories / highlights / share-links of highlights -> API only
API_ONLY_RE = re.compile(r"instagram\.com/(?:stories/|s/)", re.IGNORECASE)


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
    url = (url or "").strip().rstrip(".,!?);]")
    if not IG_RE.search(url):
        raise IGError("Valid Instagram Reel/Post/Story link bhejo.")
    return url


# ---------------------------------------------------------------------------
# yt-dlp (primary for public reels / posts)
# ---------------------------------------------------------------------------
def _yt_dlp_worker(link: str, folder: str, all_slides: bool) -> IGResult:
    """Synchronous yt-dlp worker, run in a thread so the bot event loop stays responsive."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise IGError("yt-dlp install nahi hai (pip install -U yt-dlp).") from exc

    opts = {
        "outtmpl": os.path.join(folder, "%(id)s_%(autonumber)03d.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": not all_slides,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 25,
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

            files = []
            seen = set()
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
                for candidate in candidates:
                    candidate = os.path.abspath(candidate)
                    if candidate not in seen and os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                        seen.add(candidate)
                        files.append((candidate, entry))
                        break

            if not files:
                for p in sorted(Path(folder).iterdir()):
                    if p.is_file() and p.stat().st_size > 0 and p.suffix.lower() not in (".json", ".part"):
                        files.append((str(p), info))

            if not files:
                raise IGError("yt-dlp ne download kiya, lekin file locate nahi hui.")

            result = []
            for path, _entry in files:
                ext = Path(path).suffix.lower()
                kind = "photo" if ext in (".jpg", ".jpeg", ".png", ".webp") else "video"
                result.append(IGItem(path=path, kind=kind))
            title = str(info.get("title") or info.get("description") or "Instagram media")
            total = int(info.get("playlist_count") or info.get("n_entries") or len(result) or 1)
            return IGResult(url=link, title=title, total=max(total, len(result)), items=result)
    except IGError:
        raise
    except Exception as exc:
        message = str(exc).replace("\n", " ")[:350]
        raise IGError(f"yt-dlp fail: {message}") from exc


# ---------------------------------------------------------------------------
# RiteshYT API (fallback + stories / highlights)
# ---------------------------------------------------------------------------
def _walk_dicts(obj):
    """Yield dictionaries nested in a JSON response, without assuming one schema."""
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


def _media_urls(payload):
    """Extract common media URL shapes from API responses (carousel, stories, single media)."""
    found = []
    seen = set()
    objects = list(_walk_dicts(payload))

    for obj in objects:
        for key in ("items", "media", "carousel_media", "media_urls", "resources", "results", "stories"):
            value = obj.get(key)
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, str):
                        candidate = _normalize_media_url(entry)
                        if candidate:
                            found.append((candidate, ""))
                    elif isinstance(entry, dict):
                        nested = list(_walk_dicts(entry))
                        candidate = _first_value(nested, (
                            "download_url", "video_url", "videoUrl", "image_url",
                            "imageUrl", "display_url", "displayUrl", "media_url",
                            "mediaUrl", "src", "url", "thumbnail_url",
                        ))
                        candidate = _normalize_media_url(candidate)
                        kind = str(_first_value(nested, ("type", "media_type", "kind")) or "").lower()
                        if candidate:
                            found.append((candidate, kind))

    for obj in objects:
        for key in (
            "download_url", "video_url", "videoUrl", "image_url", "imageUrl",
            "display_url", "displayUrl", "media_url", "mediaUrl", "direct_url",
        ):
            candidate = _normalize_media_url(obj.get(key))
            if candidate:
                kind = str(obj.get("type") or obj.get("media_type") or obj.get("kind") or "").lower()
                found.append((candidate, kind))

    for obj in objects:
        candidate = _normalize_media_url(obj.get("url"))
        if candidate:
            parsed = urlparse(candidate)
            if any(x in parsed.path.lower() for x in (".mp4", ".jpg", ".jpeg", ".png", ".webp", ".m4v", "/stream")):
                kind = str(obj.get("type") or obj.get("media_type") or obj.get("kind") or "").lower()
                found.append((candidate, kind))

    for candidate, kind in found:
        if candidate not in seen:
            seen.add(candidate)
            yield candidate, kind


def _add_api_key_to_stream(url: str) -> str:
    """Some API stream routes also require the same API key."""
    parsed = urlparse(url)
    api_host = urlparse(API_URL).netloc
    if parsed.netloc and parsed.netloc != api_host:
        return url
    query = parse_qs(parsed.query, keep_blank_values=True)
    if API_KEY and not query.get("api_key"):
        query["api_key"] = [API_KEY]
        parsed = parsed._replace(query=urlencode(query, doseq=True))
    return urlunparse(parsed)


def _suffix_from_url(url: str, content_type: str = "") -> str:
    path = urlparse(url).path.lower()
    for ext in (".mp4", ".m4v", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ext
    ctype = (content_type or "").split(";", 1)[0].lower()
    return {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ctype, ".mp4")


async def _download_url(client: httpx.AsyncClient, url: str, folder: str, index: int) -> IGItem:
    url = _add_api_key_to_stream(url)
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=90) as response:
            response.raise_for_status()
            ctype = response.headers.get("content-type", "")
            if "application/json" in ctype.lower() or "text/html" in ctype.lower():
                body = (await response.aread())[:500].decode("utf-8", "replace")
                raise IGError(f"API media endpoint ne file ke bajaye response diya: {body[:250]}")
            suffix = _suffix_from_url(url, ctype)
            path = os.path.join(folder, f"instagram_{index}{suffix}")
            with open(path, "wb") as out:
                async for chunk in response.aiter_bytes():
                    out.write(chunk)
        if not os.path.exists(path) or os.path.getsize(path) < 128:
            raise IGError("Empty ya invalid media file mili.")
        kind = "photo" if suffix in (".jpg", ".jpeg", ".png", ".webp") else "video"
        return IGItem(path=path, kind=kind)
    except httpx.HTTPStatusError as exc:
        raise IGError(f"Media download HTTP {exc.response.status_code}; API stream URL/access check karo.") from exc
    except httpx.HTTPError as exc:
        raise IGError(f"Media download network error: {exc}") from exc


async def _api_download(link: str, folder: str, all_slides: bool) -> IGResult:
    if not API_KEY:
        raise IGError("API_KEY environment variable set nahi hai.")
    endpoint = f"{API_URL}/api/platform/extract"
    timeout = httpx.Timeout(45.0, connect=15.0)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MediaDownloader/1.0)"}

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        try:
            response = await client.get(endpoint, params={"url": link, "api_key": API_KEY})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise IGError("Instagram API ne key/access reject kiya (HTTP 401/403).") from exc
            if code == 429:
                raise IGError("Instagram API rate limit (HTTP 429); thodi der baad try karo.") from exc
            raise IGError(f"Instagram API HTTP {code}.") from exc
        except httpx.HTTPError as exc:
            raise IGError(f"Instagram API connect nahi hua: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise IGError("Instagram API ne valid JSON response nahi diya.") from exc

        objects = list(_walk_dicts(payload))
        success = _first_value(objects, ("success", "ok"))
        if success is False:
            message = _first_value(objects, ("message", "detail", "error", "reason"))
            raise IGError(f"Instagram API: {str(message or 'request failed')[:300]}")

        title = str(_first_value(objects, ("title", "caption", "description", "shortcode")) or "Instagram media")
        canonical = str(_first_value(objects, ("permalink", "canonical_url", "post_url")) or link)
        total_value = _first_value(objects, ("total", "media_count", "carousel_count", "count"))
        try:
            total = max(1, int(total_value))
        except (TypeError, ValueError):
            total = 1

        media = list(_media_urls(payload))
        if not media:
            message = _first_value(objects, ("message", "detail", "error", "reason"))
            raise IGError(f"API response mein media URL nahi mila. {str(message or '')[:200]}")

        if not all_slides and total > 1:
            media = media[:1]

        items = []
        for i, (url, kind_hint) in enumerate(media, 1):
            item = await _download_url(client, url, folder, i)
            if kind_hint in ("image", "photo", "picture"):
                item.kind = "photo"
            elif kind_hint in ("video", "reel", "animation"):
                item.kind = "video"
            items.append(item)

        if not items:
            raise IGError("Instagram API se koi media download nahi hui.")
        return IGResult(url=canonical, title=title, total=max(total, len(items)), items=items)


# ---------------------------------------------------------------------------
# Public entry point (same signature the bot already uses)
# ---------------------------------------------------------------------------
def _reset_folder(folder: str):
    """Remove partial files left by a failed attempt so the next one starts clean."""
    for p in Path(folder).iterdir():
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


async def fetch_instagram(url: str, folder: str, all_slides: bool = False) -> IGResult:
    link = _clean_url(url)
    os.makedirs(folder, exist_ok=True)

    # Stories / highlights: yt-dlp can't do these without login -> API only
    if API_ONLY_RE.search(link):
        return await _api_download(link, folder, all_slides)

    # Public reel / post: yt-dlp first, API as fallback
    try:
        return await asyncio.to_thread(_yt_dlp_worker, link, folder, all_slides)
    except Exception as exc:
        ytdlp_error = exc
        log.warning("yt-dlp failed, trying API (%s)", exc)

    _reset_folder(folder)
    try:
        return await _api_download(link, folder, all_slides)
    except Exception as api_error:
        raise IGError(
            f"yt-dlp: {str(ytdlp_error)[:150]} | API: {str(api_error)[:200]}"
        ) from api_error
