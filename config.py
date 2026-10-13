import os
import re

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_URL = os.getenv("OWNER_URL", "")      # e.g. https://t.me/yourusername
CHANNEL_URL = os.getenv("CHANNEL_URL", "")  # e.g. https://t.me/yourchannel
MAX_FILE_MB = 50                            # Bot API upload limit
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "5"))

# Owner(s) allowed to use /broadcast and /stats.
# Heroku config: OWNER_ID=123456789   (multiple: 123456789,987654321)
OWNER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("OWNER_ID", "")) if x.strip().isdigit()
}

# MongoDB for users/groups list (broadcast). Heroku config: MONGO_URI=mongodb+srv://...
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "pinbot")

# Premium emoji IDs (built in). Env EMOJI_<NAME> overrides any of them.
EMOJI_IDS = {
    "pin": "5893450623449305489",
    "fire": "5893450623449305489",
    "link": "5902449142575141204",
    "help": "5798743308922523679",
    "ok": "5447153029759397673",
    "err": "5449913031578379785",
    "warn": "5240241223632954241",
    "load": "5375365992891312468",
    "delete": "5904542823167824187",
    "back": "5447389832781264371",
    "owner": "6077842923905357935",
    "channel": "6269303009658802514",
}


def eid(name: str):
    """Premium emoji id for a name (used for button icons too)."""
    env = re.sub(r"\D", "", os.getenv(f"EMOJI_{name.upper()}", ""))  # allows "[123]" style
    return env or EMOJI_IDS.get(name)


def em(name: str, fallback: str) -> str:
    """Premium emoji inside message text (HTML). Falls back to normal emoji."""
    emoji_id = eid(name)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback
