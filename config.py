import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_URL = os.getenv("OWNER_URL", "")      # e.g. https://t.me/yourusername
CHANNEL_URL = os.getenv("CHANNEL_URL", "")  # e.g. https://t.me/yourchannel
MAX_FILE_MB = 50                            # Bot API upload limit
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "5"))


def em(name: str, fallback: str) -> str:
    """Premium emoji inside message text (HTML). Falls back to normal emoji."""
    emoji_id = os.getenv(f"EMOJI_{name.upper()}", "").strip()
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


def eid(name: str):
    """Premium emoji id for inline button icon (or None)."""
    return os.getenv(f"EMOJI_{name.upper()}", "").strip() or None
