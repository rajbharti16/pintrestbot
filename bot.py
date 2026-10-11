import asyncio
import html
import logging
import os
import shutil
import tempfile

from config import BOT_TOKEN, CHANNEL_URL, MAX_PARALLEL, OWNER_URL, eid, em
from pinterest import PIN_RE, PinError, download, fetch_media
from tg import Tg, TgError, btn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # URLs contain the bot token
log = logging.getLogger("pinbot")

tg = Tg(BOT_TOKEN)
sem = asyncio.Semaphore(MAX_PARALLEL)


# ---------- texts & keyboards ----------
def start_text() -> str:
    return (
        f"{em('pin', '📌')} <b>Pinterest Downloader</b>\n\n"
        f"Koi bhi Pinterest pin ka link bhejo, main video ya photo "
        f"download karke de dunga {em('fire', '🔥')}\n\n"
        f"{em('link', '🔗')} <i>pinterest.com / pin.it dono links chalte hain</i>"
    )


def help_text() -> str:
    return (
        f"{em('help', '❓')} <b>Kaise use kare</b>\n\n"
        f"1. Pinterest app me pin kholo\n"
        f"2. <b>Share → Copy link</b> dabao\n"
        f"3. Link yaha bhej do\n\n"
        f"{em('ok', '✅')} Video, photo aur GIF teeno support hain."
    )


def start_kb() -> dict:
    rows = [[btn("How to use", cb="help", style="primary", emoji_id=eid("help"))]]
    extra = []
    if OWNER_URL:
        extra.append(btn("Owner", url=OWNER_URL, style="success", emoji_id=eid("owner")))
    if CHANNEL_URL:
        extra.append(btn("Channel", url=CHANNEL_URL, style="primary", emoji_id=eid("channel")))
    if extra:
        rows.append(extra)
    return {"inline_keyboard": rows}


def help_kb() -> dict:
    return {"inline_keyboard": [[btn("Back", cb="back", style="primary", emoji_id=eid("back"))]]}


def result_kb(pin_url: str) -> dict:
    return {
        "inline_keyboard": [
            [
                btn("Open Pin", url=pin_url, style="primary", emoji_id=eid("link")),
                btn("Delete", cb="del", style="danger", emoji_id=eid("delete")),
            ]
        ]
    }


# ---------- handlers ----------
async def on_message(m: dict):
    chat_id = m["chat"]["id"]
    text = m.get("text") or m.get("caption") or ""

    # helper: send a premium emoji to the bot and it replies with its id
    ids = [
        e["custom_emoji_id"]
        for e in (m.get("entities") or [])
        if e.get("type") == "custom_emoji"
    ]
    if ids:
        await tg.call(
            "sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="\n".join(f"<code>{i}</code>" for i in ids),
        )
        return

    if text.startswith(("/start", "/help")):
        await tg.call(
            "sendMessage", chat_id=chat_id, text=start_text(),
            parse_mode="HTML", reply_markup=start_kb(),
        )
        return

    match = PIN_RE.search(text)
    if not match:
        if m["chat"]["type"] == "private":
            await tg.call(
                "sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=f"{em('warn', '⚠️')} Valid Pinterest link bhejo.",
            )
        return

    wait = await tg.call(
        "sendMessage", chat_id=chat_id, parse_mode="HTML",
        reply_parameters={"message_id": m["message_id"]},
        text=f"{em('load', '⏳')} <b>Processing...</b>",
    )
    tmp = tempfile.mkdtemp(prefix="pin_")
    try:
        async with sem:
            media = await fetch_media(match.group(0))
            path = await download(media, tmp)

        caption = f"{em('ok', '✅')} <b>Downloaded</b>"
        if media.title:
            caption += f"\n{html.escape(media.title[:200])}"

        method, field = {
            "video": ("sendVideo", "video"),
            "photo": ("sendPhoto", "photo"),
            "gif": ("sendAnimation", "animation"),
        }[media.kind]
        if media.kind == "photo" and os.path.getsize(path) > 10 * 1024 * 1024:
            method, field = "sendDocument", "document"  # sendPhoto limit is 10MB

        extra = {"supports_streaming": True} if media.kind == "video" else {}
        send_args = dict(
            chat_id=chat_id, caption=caption, parse_mode="HTML",
            reply_markup=result_kb(media.pin_url),
            reply_parameters={"message_id": m["message_id"]},
        )
        try:
            await tg.call(method, file=(field, path), **send_args, **extra)
        except TgError:
            if method == "sendDocument":
                raise
            # e.g. bad photo dimensions/format -> send as a file instead
            await tg.call("sendDocument", file=("document", path), **send_args)
    except (PinError, TgError) as e:
        await tg.call(
            "sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=f"{em('err', '❌')} {html.escape(str(e))}",
        )
    except Exception:
        log.exception("download failed")
        await tg.call(
            "sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=f"{em('err', '❌')} Kuch gadbad ho gayi, thodi der baad try karo.",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await tg.call("deleteMessage", chat_id=chat_id, message_id=wait["message_id"])
        except TgError:
            pass


async def on_callback(c: dict):
    msg = c.get("message") or {}
    chat_id, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
    data = c.get("data")
    await tg.call("answerCallbackQuery", callback_query_id=c["id"])
    if not chat_id:
        return
    if data == "del":
        await tg.call("deleteMessage", chat_id=chat_id, message_id=mid)
    elif data == "help":
        await tg.call(
            "editMessageText", chat_id=chat_id, message_id=mid,
            text=help_text(), parse_mode="HTML", reply_markup=help_kb(),
        )
    elif data == "back":
        await tg.call(
            "editMessageText", chat_id=chat_id, message_id=mid,
            text=start_text(), parse_mode="HTML", reply_markup=start_kb(),
        )


async def safe(coro):
    try:
        await coro
    except Exception:
        log.exception("handler error")


async def main():
    me = await tg.call("getMe")
    await tg.call("deleteWebhook")
    log.info("Started as @%s", me["username"])
    offset = 0
    while True:
        try:
            updates = await tg.call(
                "getUpdates", offset=offset, timeout=30,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as e:
            log.warning("getUpdates failed: %s", e)
            await asyncio.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            if "message" in u:
                asyncio.create_task(safe(on_message(u["message"])))
            elif "callback_query" in u:
                asyncio.create_task(safe(on_callback(u["callback_query"])))


if __name__ == "__main__":
    asyncio.run(main())
