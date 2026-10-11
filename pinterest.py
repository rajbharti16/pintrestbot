import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass

import httpx

from config import MAX_FILE_MB

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PIN_RE = re.compile(
    r"https?://(?:[\w-]+\.)?pinterest\.[a-z.]+/pin/[^\s]+|https?://pin\.it/[\w-]+",
    re.I,
)
ID_RE = re.compile(r"/pin/(?:[^/\s?]*--)?(\d+)")
VIDEO_PREF = ["V_720P", "V_EXP7", "V_EXP6", "V_EXP5", "V_EXP4", "V_EXP3", "V_360P"]


class PinError(Exception):
    pass


@dataclass
class Media:
    kind: str  # video | photo | gif
    url: str
    title: str
    pin_url: str


async def _resolve_id(client: httpx.AsyncClient, url: str) -> str:
    m = ID_RE.search(url)
    if not m:  # pin.it short link -> follow redirects
        r = await client.get(url, follow_redirects=True)
        m = ID_RE.search(str(r.url))
    if not m:
        raise PinError("Valid Pinterest pin link nahi mila.")
    return m.group(1)


async def _pick_video(client: httpx.AsyncClient, vl: dict):
    for key in VIDEO_PREF:
        if key in vl and vl[key].get("url", "").endswith(".mp4"):
            return vl[key]["url"]
    for v in vl.values():
        if v.get("url", "").endswith(".mp4"):
            return v["url"]
    for v in vl.values():  # only HLS available
        u = v.get("url", "")
        if u.endswith(".m3u8"):
            guess = u.replace("/hls/", "/720p/").replace(".m3u8", ".mp4")
            try:
                r = await client.get(
                    guess,
                    headers={"User-Agent": UA, "Range": "bytes=0-1",
                             "Referer": "https://www.pinterest.com/"},
                )
                if r.status_code in (200, 206):
                    return guess
            except httpx.HTTPError:
                pass
            return u  # ffmpeg will convert this
    return None


IMG_OK = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _pick_image(images: dict):
    orig = (images.get("orig") or {}).get("url", "")
    if orig.lower().endswith(IMG_OK):
        return orig
    # original is .heic etc. -> take the largest jpg/png variant instead
    cands = [v for v in images.values() if v.get("url", "").lower().endswith(IMG_OK)]
    cands.sort(key=lambda v: v.get("width", 0), reverse=True)
    if cands:
        return cands[0]["url"]
    if orig:
        return re.sub(r"\.\w+$", ".jpg", orig)
    return None


def _ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # bundled binary from pip (works on Heroku without buildpack)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise PinError("ffmpeg available nahi hai (pip install imageio-ffmpeg).")


def _find_video_list(data: dict):
    if (data.get("videos") or {}).get("video_list"):
        return data["videos"]["video_list"]
    for page in (data.get("story_pin_data") or {}).get("pages", []):
        for block in page.get("blocks", []):
            vl = (block.get("video") or {}).get("video_list")
            if vl:
                return vl
    return None


async def _via_api(client, pid: str):
    params = {
        "source_url": f"/pin/{pid}/",
        "data": json.dumps({"options": {"id": pid, "field_set_key": "detailed"}, "context": {}}),
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-PWS-Handler": "www/pin/[id].js",
    }
    r = await client.get(
        "https://www.pinterest.com/resource/PinResource/get/",
        params=params, headers=headers,
    )
    data = r.json()["resource_response"]["data"]
    title = data.get("title") or data.get("grid_title") or ""

    vl = _find_video_list(data)
    if vl:
        url = await _pick_video(client, vl)
        if url:
            return Media("video", url, title, f"https://www.pinterest.com/pin/{pid}/")

    img_url = _pick_image(data.get("images") or {})
    if img_url:
        kind = "gif" if img_url.lower().endswith(".gif") else "photo"
        return Media(kind, img_url, title, f"https://www.pinterest.com/pin/{pid}/")
    return None


async def _via_html(client, pid: str):
    pin_url = f"https://www.pinterest.com/pin/{pid}/"
    html = (await client.get(pin_url, headers={"User-Agent": UA})).text
    mp4 = re.search(r"https://v\d*\.pinimg\.com/videos/[^\"\\\s]+?\.mp4", html)
    t = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    title = t.group(1) if t else ""
    if mp4:
        return Media("video", mp4.group(0), title, pin_url)
    img = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if img:
        url = re.sub(r"/\d+x/", "/originals/", img.group(1))
        kind = "gif" if url.endswith(".gif") else "photo"
        return Media(kind, url, title, pin_url)
    return None


async def fetch_media(link: str) -> Media:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}) as client:
        pid = await _resolve_id(client, link)
        media = None
        try:
            media = await _via_api(client, pid)
        except Exception:
            pass
        if not media:
            media = await _via_html(client, pid)
        if not media:
            raise PinError("Is pin me koi video/photo nahi mila (private ya delete ho sakta hai).")
        return media


async def download(media: Media, folder: str) -> str:
    limit = MAX_FILE_MB * 1024 * 1024

    if media.url.endswith(".m3u8"):
        path = os.path.join(folder, "pin.mp4")
        proc = await asyncio.create_subprocess_exec(
            _ffmpeg_exe(), "-y", "-loglevel", "error", "-user_agent", UA, "-i", media.url,
            "-c", "copy", "-bsf:a", "aac_adtstoasc", path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(path):
            raise PinError("Video convert nahi ho paya (ffmpeg installed hai?).")
        return path

    ext = os.path.splitext(media.url.split("?")[0])[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"):
        ext = {"video": ".mp4", "photo": ".jpg", "gif": ".gif"}[media.kind]
    path = os.path.join(folder, "pin" + ext)

    size = 0
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": UA}) as client:
        async with client.stream("GET", media.url) as r:
            if r.status_code != 200:
                raise PinError("Media download fail ho gaya.")
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes(256 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise PinError(f"File {MAX_FILE_MB}MB se badi hai, Telegram bot bhej nahi sakta.")
                    f.write(chunk)
    return path
