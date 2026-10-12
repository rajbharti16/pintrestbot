"""Broadcast + user/group tracking.

Owner commands:
  /broadcast [-user] [-gc] [-pin]   -> reply to a message, or write text after the flags
  /stats                            -> number of users and groups

  -user : sirf users (private chats)
  -gc   : sirf groups
  (koi bhi nahi / dono) : users + groups dono
  -pin  : groups me message pin bhi kar do (bot ko group me pin permission chahiye)

Storage: MongoDB if MONGO_URI is set, otherwise a JSON file (Heroku restart pe
JSON file mit jati hai, isliye Heroku pe MONGO_URI use karo).
"""
import asyncio
import json
import logging
import os
import re

from tg import TgError

log = logging.getLogger("pinbot")

OWNER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("OWNER_ID", "")) if x.strip().isdigit()
}
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DATA_FILE = os.getenv("CHATS_FILE", "chats.json")

# errors that mean "this chat is gone" -> remove it from the list
DEAD = (
    "forbidden", "blocked", "deactivated", "kicked", "chat not found",
    "not a member", "was deleted", "upgraded to a supergroup", "user not found",
    "peer_id_invalid",
)

FLAGS_RE = re.compile(r"^((?:-(?:user|gc|pin|all)\b\s*)*)(.*)$", re.S | re.I)

USAGE = (
    "Usage:\n"
    "/broadcast [-user] [-gc] [-pin]\n\n"
    "- Kisi message pe reply karke command do, ya flags ke baad text likho.\n"
    "- -user: sirf users\n"
    "- -gc: sirf groups\n"
    "- koi flag nahi: users + groups dono\n"
    "- -pin: groups me message pin bhi karega"
)


class Store:
    def __init__(self):
        self.seen = {}  # chat_id -> "user" | "group"
        self.mongo = None
        if MONGO_URI:
            from pymongo import AsyncMongoClient

            db = AsyncMongoClient(MONGO_URI)[os.getenv("MONGO_DB", "pinbot")]
            self.mongo = db["chats"]
        else:
            log.warning("MONGO_URI not set: using %s (restart pe data ud sakta hai)", DATA_FILE)
            self._load()

    def _load(self):
        try:
            with open(DATA_FILE) as f:
                self.seen = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            self.seen = {}

    def _save(self):
        try:
            with open(DATA_FILE, "w") as f:
                json.dump({str(k): v for k, v in self.seen.items()}, f)
        except OSError:
            log.exception("could not save %s", DATA_FILE)

    async def add(self, cid: int, kind: str):
        if self.seen.get(cid) == kind:
            return
        self.seen[cid] = kind
        if self.mongo is not None:
            await self.mongo.update_one({"_id": cid}, {"$set": {"kind": kind}}, upsert=True)
        else:
            self._save()

    async def remove(self, cid: int):
        had = self.seen.pop(cid, None) is not None
        if self.mongo is not None:
            await self.mongo.delete_one({"_id": cid})
        elif had:
            self._save()

    async def ids(self, kind: str):
        if self.mongo is not None:
            return [d["_id"] async for d in self.mongo.find({"kind": kind}, {"_id": 1})]
        return [c for c, k in self.seen.items() if k == kind]


class Broadcaster:
    def __init__(self, tg):
        self.tg = tg
        self.store = Store()
        self.running = False
        self._task = None

    # ---- tracking ----
    async def track(self, m: dict):
        t = m["chat"]["type"]
        if t == "private":
            await self.store.add(m["chat"]["id"], "user")
        elif t in ("group", "supergroup"):
            await self.store.add(m["chat"]["id"], "group")

    async def membership(self, u: dict):
        """my_chat_member update: bot added to / removed from a chat, user blocked bot."""
        chat = u["chat"]
        status = u["new_chat_member"]["status"]
        if chat["type"] == "private":
            kind = "user"
        elif chat["type"] in ("group", "supergroup"):
            kind = "group"
        else:
            return
        if status in ("kicked", "left"):
            await self.store.remove(chat["id"])
        else:
            await self.store.add(chat["id"], kind)

    # ---- commands ----
    async def handle(self, m: dict) -> bool:
        """Returns True if the message was a broadcast/stats command (handled)."""
        text = m.get("text") or ""
        if not text.startswith("/"):
            return False
        cmd = text.split(maxsplit=1)[0].split("@")[0].lower()
        if cmd not in ("/broadcast", "/stats"):
            return False

        chat_id = m["chat"]["id"]
        uid = (m.get("from") or {}).get("id")
        if uid not in OWNER_IDS:
            return True  # ignore silently

        if cmd == "/stats":
            u, g = len(await self.store.ids("user")), len(await self.store.ids("group"))
            await self.tg.call(
                "sendMessage", chat_id=chat_id,
                text=f"Users: {u}\nGroups: {g}\nTotal: {u + g}",
            )
            return True

        parts = text.split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""
        fm = FLAGS_RE.match(rest)
        flags = {f.lower() for f in re.findall(r"-\w+", fm.group(1))}
        body = fm.group(2).strip()

        reply = m.get("reply_to_message")
        if reply:
            src = ("copy", reply["message_id"])
        elif body:
            src = ("text", body)
        else:
            await self.tg.call("sendMessage", chat_id=chat_id, text=USAGE)
            return True

        if self.running:
            await self.tg.call("sendMessage", chat_id=chat_id, text="Ek broadcast already chal raha hai.")
            return True

        want_user = "-user" in flags
        want_gc = "-gc" in flags
        if "-all" in flags or not (want_user or want_gc):
            want_user = want_gc = True
        pin = "-pin" in flags

        self._task = asyncio.create_task(self._run(chat_id, src, want_user, want_gc, pin))
        return True

    # ---- sending ----
    async def _send_one(self, cid: int, src: tuple, owner_chat: int):
        for attempt in (1, 2):
            try:
                if src[0] == "copy":
                    return await self.tg.call(
                        "copyMessage", chat_id=cid, from_chat_id=owner_chat, message_id=src[1]
                    )
                return await self.tg.call(
                    "sendMessage", chat_id=cid, text=src[1], parse_mode="HTML",
                    link_preview_options={"is_disabled": True},
                )
            except TgError as e:
                wait = re.search(r"retry after (\d+)", str(e))
                if wait and attempt == 1:
                    await asyncio.sleep(int(wait.group(1)) + 1)
                    continue
                raise

    async def _run(self, owner_chat: int, src: tuple, want_user: bool, want_gc: bool, pin: bool):
        self.running = True
        users = await self.store.ids("user") if want_user else []
        groups = await self.store.ids("group") if want_gc else []
        st = {"user": 0, "group": 0, "failed": 0, "removed": 0, "pinned": 0, "pin_failed": 0}
        total = len(users) + len(groups)

        status = await self.tg.call(
            "sendMessage", chat_id=owner_chat,
            text=f"Broadcast start: {len(users)} users, {len(groups)} groups"
                 + (" (+pin)" if pin else ""),
        )

        def summary(prefix: str) -> str:
            s = (
                f"{prefix}\n\nUsers: {st['user']}/{len(users)}\n"
                f"Groups: {st['group']}/{len(groups)}\n"
                f"Failed: {st['failed']} (removed: {st['removed']})"
            )
            if pin:
                s += f"\nPinned: {st['pinned']} (pin fail: {st['pin_failed']})"
            return s

        done = 0
        try:
            for kind, ids in (("user", users), ("group", groups)):
                for cid in ids:
                    try:
                        res = await self._send_one(cid, src, owner_chat)
                        st[kind] += 1
                        if pin and kind == "group":
                            try:
                                await self.tg.call(
                                    "pinChatMessage", chat_id=cid,
                                    message_id=res["message_id"], disable_notification=True,
                                )
                                st["pinned"] += 1
                            except TgError:
                                st["pin_failed"] += 1  # bot ke paas pin right nahi
                    except TgError as e:
                        st["failed"] += 1
                        if any(k in str(e).lower() for k in DEAD):
                            st["removed"] += 1
                            await self.store.remove(cid)
                        else:
                            log.info("broadcast to %s failed: %s", cid, e)
                    done += 1
                    await asyncio.sleep(0.05)  # stay under Telegram's ~30 msg/sec
                    if done % 200 == 0:
                        try:
                            await self.tg.call(
                                "editMessageText", chat_id=owner_chat,
                                message_id=status["message_id"],
                                text=summary(f"Running... {done}/{total}"),
                            )
                        except TgError:
                            pass
        except Exception:
            log.exception("broadcast crashed")
        finally:
            self.running = False
            try:
                await self.tg.call(
                    "editMessageText", chat_id=owner_chat,
                    message_id=status["message_id"], text=summary("Broadcast done"),
                )
            except TgError:
                pass
