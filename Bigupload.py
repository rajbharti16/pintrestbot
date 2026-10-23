"""Upload files bigger than 50MB (up to 2GB) through MTProto using Telethon.

Telegram's normal Bot API only accepts 50MB uploads, but the same bot token can
log in over MTProto, where the limit is 2GB.

Needs API_ID and API_HASH from https://my.telegram.org (API development tools).
Without them this module stays disabled and the bot sends big files in parts.
"""
import logging

from config import API_HASH, API_ID, BOT_TOKEN

log = logging.getLogger("pinbot")
logging.getLogger("telethon").setLevel(logging.WARNING)


class BigUploader:
    def __init__(self):
        self.client = None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def start(self):
        if not (API_ID and API_HASH):
            log.info("API_ID/API_HASH not set: big files will be sent in parts")
            return
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        self.client = client
        log.info("MTProto uploader ready (files up to 2GB)")

    async def send(self, chat_id: int, path: str, kind: str, title: str = "",
                   performer: str = "", duration: int = 0, width: int = 0,
                   height: int = 0, reply_to=None) -> int:
        """Upload `path` to chat_id and return the new message id."""
        from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

        if kind == "audio":
            attrs = [DocumentAttributeAudio(
                duration=int(duration or 0), title=title[:60], performer=performer[:60],
            )]
        else:
            attrs = [DocumentAttributeVideo(
                duration=int(duration or 0), w=int(width or 1280), h=int(height or 720),
                supports_streaming=True,
            )]
        msg = await self.client.send_file(
            chat_id, path, caption=title[:200], attributes=attrs,
            reply_to=reply_to, supports_streaming=(kind != "audio"), force_document=False,
        )
        return msg.id
