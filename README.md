# Pinterest Downloader Telegram Bot

Pinterest pin ka link bhejo, video / photo / GIF download ho jata hai.
Colored inline buttons + premium emoji support.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env     # BOT_TOKEN daalo
python bot.py
```

Video ke liye `ffmpeg` install rakho (sirf tab chahiye jab Pinterest sirf HLS/m3u8 de).
Docker: `docker build -t pinbot . && docker run --env-file .env pinbot`

## Files

| File | Kaam |
|---|---|
| `bot.py` | handlers, buttons, polling loop |
| `pinterest.py` | link resolve, media extract, download |
| `tg.py` | raw Bot API client + `btn()` helper |
| `config.py` | env + premium emoji helpers |

## Colored buttons

`btn(text, style=...)` me `primary` (blue), `success` (green), `danger` (red).
Ye naye Bot API fields hain, isliye raw API use kiya hai. Agar kisi wajah se Telegram
reject kare to `tg.py` automatically bina color ke retry karta hai.

## Premium emoji

1. Bot ko koi premium emoji bhejo, wo uski ID reply karega.
2. `.env` me `EMOJI_*` me ID paste karo.
3. Message text me `<tg-emoji>` tag use hota hai, buttons me `icon_custom_emoji_id`.

Note: custom emoji har bot/account pe render hone ki guarantee nahi hoti (Telegram ki
conditions lagti hain, jaise bot owner ka Premium hona). Render na ho to normal emoji
fallback dikhta hai, bot kaam karta rahega.

## Limits

- Bot API upload limit 50MB.
- Private pins download nahi honge.
- Pinterest apna format badalta rehta hai, agar extraction toot jaye to `pinterest.py` update karna padega.
