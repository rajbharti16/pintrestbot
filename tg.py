"""Minimal raw Telegram Bot API client.

Raw calls are used on purpose: colored buttons (`style`) and premium emoji on
buttons (`icon_custom_emoji_id`) are new Bot API fields, and many libraries
don't expose them yet. Here you can pass any field straight through.
"""
import json

import httpx


class TgError(Exception):
    pass


def _strip(markup: dict) -> dict:
    for row in markup.get("inline_keyboard", []):
        for b in row:
            b.pop("style", None)
            b.pop("icon_custom_emoji_id", None)
    return markup


def btn(text, *, url=None, cb=None, style=None, emoji_id=None) -> dict:
    """style: 'primary' (blue) | 'success' (green) | 'danger' (red)"""
    b = {"text": text}
    if url:
        b["url"] = url
    if cb:
        b["callback_data"] = cb
    if style:
        b["style"] = style
    if emoji_id:
        b["icon_custom_emoji_id"] = emoji_id
    return b


class Tg:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15))

    async def call(self, method: str, file=None, **params):
        for attempt in (1, 2):
            data = {
                k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                for k, v in params.items()
                if v is not None
            }
            if file:
                field, path = file
                with open(path, "rb") as f:
                    r = await self.http.post(self.base + method, data=data, files={field: f})
            else:
                r = await self.http.post(self.base + method, data=data)

            js = r.json()
            if js.get("ok"):
                return js["result"]

            markup = params.get("reply_markup")
            if attempt == 1 and r.status_code == 400 and isinstance(markup, dict):
                _strip(markup)  # retry without color/icon fields
                continue
            raise TgError(js.get("description", "unknown error"))
