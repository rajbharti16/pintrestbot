import asyncio
import html
import logging
import os
import shutil
import tempfile

from config import BOT_TOKEN, CHANNEL_URL, MAX_PARALLEL, OWNER_URL, eid, em
from broadcast import Broadcaster
from instagram import IG_RE, IGError, fetch_instagram
from pinterest import PIN_RE, PinError, download, fetch_media
from tg import Tg, TgError, btn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # URLs contain the bot token
log = logging.getLogger("pinbot")

tg = Tg(BOT_TOKEN)
bc = Broadcaster(tg)
BOT = {"username": ""}
sem = asyncio.Semaphore(MAX_PARALLEL)


# ---------- texts & keyboards ----------
def start_text(name: str = "") -> str:
    hi = html.escape(name) if name else "dost"
    return (
        f"{em('pin', '📌')} <b>Media Downloader Bot</b>\n\n"
        f"Hey <b>{hi}</b> {em('fire', '🔥')}\n"
        f"Instagram ya Pinterest ka link bhejo, main turant video / photo "
        f"download karke de dunga.\n\n"
        f"<blockquote>"
        f"{em('ok', '✅')} <b>Instagram</b> — Reels, Posts, Stories\n"
        f"{em('ok', '✅')} <b>Pinterest</b> — Video, Photo, GIF\n"
        f"{em('ok', '✅')} <b>Groups</b> — link daalo, video turant aa jayegi"
        f"</blockquote>\n\n"
        f"{em('link', '🔗')} <i>Bas link paste karo, baaki mujh par chhodo!</i>"
    )


def help_text() -> str:
    return (
        f"{em('help', '❓')} <b>Kaise use kare</b>\n\n"
        f"1. Instagram ya Pinterest me post / reel / pin kholo\n"
        f"2. <b>Share → Copy link</b> dabao\n"
        f"3. Link yaha (ya group me) bhej do\n\n"
        f"{em('ok', '✅')} Video, photo, GIF aur carousel support hain.\n"
        f"{em('warn', '⚠️')} Story ke liye bot owner ko Instagram session set karna padta hai."
    )


def start_kb() -> dict:
    rows = []
    if BOT["username"]:
        rows.append([btn(
            "Add me to your Group",
            url=f"https://t.me/{BOT['username']}?startgroup=true",
            style="success", emoji_id=eid("fire"),
        )])
    rows.append([btn("How to use", cb="help", style="primary", emoji_id=eid("help"))])
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


def result_kb(pin_url: str, uid=None) -> dict:
    return {
        "inline_keyboard": [
            [
                btn("Open Pin", url=pin_url, style="primary", emoji_id=eid("link")),
                btn("Delete", cb=f"del:{uid}" if uid else "del", style="danger", emoji_id=eid("delete")),
            ]
        ]
    }


# ---------- handlers ----------
async def on_message(m: dict):
    chat_id = m["chat"]["id"]
    text = m.get("text") or m.get("caption") or ""
    if (m.get("from") or {}).get("is_bot"):
        return
    uid = (m.get("from") or {}).get("id")
    # links hidden behind normal text (hyperlinks) live in the entities
    for e in (m.get("entities") or []) + (m.get("caption_entities") or []):
        if e.get("type") == "text_link" and e.get("url"):
            text += "\n" + e["url"]

    await bc.track(m)  # remember users / groups for broadcast
    if await bc.handle(m):  # /broadcast, /stats (owner only)
        return

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
            "sendMessage", chat_id=chat_id,
            text=start_text((m.get("from") or {}).get("first_name", "")),
            parse_mode="HTML", reply_markup=start_kb(),
        )
        return

    ig_match = IG_RE.search(text)
    if ig_match:
        await handle_instagram(m, ig_match.group(0))
        return

    match = PIN_RE.search(text)
    if not match:
        if m["chat"]["type"] == "private":
            await tg.call(
                "sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=f"{em('warn', '⚠️')} Pinterest ya Instagram ka valid link bhejo.",
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
            reply_markup=result_kb(media.pin_url, uid),
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


async def send_media(chat_id, kind, path, caption, markup, reply_to):
    method, field = ("sendVideo", "video") if kind == "video" else ("sendPhoto", "photo")
    if kind == "photo" and os.path.getsize(path) > 10 * 1024 * 1024:
        method, field = "sendDocument", "document"
    args = dict(
        chat_id=chat_id, caption=caption, parse_mode="HTML",
        reply_markup=markup, reply_parameters={"message_id": reply_to},
    )
    extra = {"supports_streaming": True} if kind == "video" else {}
    try:
        await tg.call(method, file=(field, path), **args, **extra)
    except TgError:
        if method == "sendDocument":
            raise
        await tg.call("sendDocument", file=("document", path), **args)


async def handle_instagram(m: dict, link: str):
    chat_id = m["chat"]["id"]
    uid = (m.get("from") or {}).get("id")
    wait = await tg.call(
        "sendMessage", chat_id=chat_id, parse_mode="HTML",
        reply_parameters={"message_id": m["message_id"]},
        text=f"{em('load', '⏳')} <b>Processing...</b>",
    )
    tmp = tempfile.mkdtemp(prefix="ig_")
    try:
        async with sem:
            res = await fetch_instagram(link, tmp)

        caption = f"{em('ok', '✅')} <b>Downloaded</b>"
        if res.title:
            caption += f"\n{html.escape(res.title[:200])}"

        last = len(res.items) - 1
        for i, item in enumerate(res.items):
            # caption + buttons only on the last item of a carousel
            await send_media(
                chat_id, item.kind, item.path,
                caption if i == last else None,
                result_kb(res.url, uid) if i == last else None,
                m["message_id"],
            )
    except (IGError, TgError) as e:
        await tg.call(
            "sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=f"{em('err', '❌')} {html.escape(str(e))}",
        )
    except Exception:
        log.exception("instagram download failed")
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
    is_del = (data or "").startswith("del")
    if not is_del:
        await tg.call("answerCallbackQuery", callback_query_id=c["id"])
    if not chat_id:
        return
    if is_del:
        owner = int(data.split(":")[1]) if ":" in data else None
        clicker = c["from"]["id"]
        allowed = owner is None or clicker == owner
        if not allowed and msg["chat"]["type"] != "private":
            try:
                member = await tg.call("getChatMember", chat_id=chat_id, user_id=clicker)
                allowed = member.get("status") in ("creator", "administrator")
            except TgError:
                allowed = False
        if allowed:
            await tg.call("answerCallbackQuery", callback_query_id=c["id"])
            await tg.call("deleteMessage", chat_id=chat_id, message_id=mid)
        else:
            await tg.call(
                "answerCallbackQuery", callback_query_id=c["id"], show_alert=True,
                text="Sirf link bhejne wala ya admin delete kar sakta hai.",
            )
    elif data == "help":
        await tg.call(
            "editMessageText", chat_id=chat_id, message_id=mid,
            text=help_text(), parse_mode="HTML", reply_markup=help_kb(),
        )
    elif data == "back":
        await tg.call(
            "editMessageText", chat_id=chat_id, message_id=mid,
            text=start_text(c["from"].get("first_name", "")),
            parse_mode="HTML", reply_markup=start_kb(),
        )


async def safe(coro):
    try:
        await coro
    except Exception:
        log.exception("handler error")


async def main():
    me = await tg.call("getMe")
    BOT["username"] = me["username"]
    await tg.call("deleteWebhook")
    log.info("Started as @%s", me["username"])
    offset = 0
    while True:
        try:
            updates = await tg.call(
                "getUpdates", offset=offset, timeout=30,
                allowed_updates=["message", "callback_query", "my_chat_member"],
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
            elif "my_chat_member" in u:
                asyncio.create_task(safe(bc.membership(u["my_chat_member"])))


if __name__ == "__main__":
    asyncio.run(main())
