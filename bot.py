import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import aiohttp
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")
DEBUG_MODE         = os.getenv("DEBUG_MODE", "false").lower() == "true"

POLL_INTERVAL   = 15     # seconds between feed scans
UPDATE_INTERVAL = 30     # seconds between live stat updates
UPDATE_DURATION = 3600   # stop updating after 1 hour

# Tokens we have already alerted on
alerted_mints: set[str] = set()

# Tokens currently being live-updated
active_tokens: dict[str, dict] = {}

# Anoncoin API endpoints to try in order
ANONCOIN_ENDPOINTS = [
    "https://anoncoin.it/api/feeds",
    "https://anoncoin.it/api/v1/feeds",
    "https://anoncoin.it/api/feed",
    "https://anoncoin.it/api/trending",
    "https://anoncoin.it/api/v1/trending",
    "https://api2.anoncoin.it/feeds",
    "https://backend.anoncoin.it/feeds",
    "https://backend.anoncoin.it/v1/feeds",
]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://anoncoin.it",
    "Referer": "https://anoncoin.it/",
}

# Follower tier → emoji (matches anoncoin.it display)
FOLLOWER_TIERS = {
    "0-1k":   "⚪ 0-1k",
    "1k+":    "🟢 1k+",
    "10k+":   "🟢 10k+",
    "25k+":   "🔵 25k+",
    "50k+":   "🔵 50k+",
    "100k+":  "🟣 100k+",
    "250k+":  "🟣 250k+",
    "500k+":  "🟠 500k+",
    "1m+":    "🔴 1M+",
    "5m+":    "🔴 5M+",
    "10m+":   "🔴 10M+",
    "15m+":   "🔴 15M+",
}

SOL_PRICE_USD = 140.0

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

SEP = "――――――――――――――――――――――"


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def fmt_usd(n):
    try:
        n = float(str(n).replace("$", "").replace(",", ""))
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n/1_000:.1f}K"
        return f"${n:.2f}"
    except Exception:
        return "—"

def fmt_pct(s):
    try:
        return str(s) if s else "—"
    except Exception:
        return "—"

def fmt_num(n):
    try:
        return f"{int(float(n)):,}"
    except Exception:
        return "0"

def fmt_impressions(n):
    try:
        n = int(n)
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.0f}K"
        return str(n)
    except Exception:
        return "?"

def parse_usd_str(s):
    try:
        return float(str(s).replace("$", "").replace(",", ""))
    except Exception:
        return 0.0

def parse_iso(ts_str):
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None

def follower_tier(followers_formatted: str) -> str:
    if not followers_formatted:
        return "⚪ 0-1k"
    key = followers_formatted.lower().strip()
    return FOLLOWER_TIERS.get(key, f"⚪ {followers_formatted}")

def elapsed_str(seconds):
    seconds = abs(int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    m, s = seconds // 60, seconds % 60
    if m < 60:
        return f"{m}m {s}s ago"
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h {m}m ago"


# ═══════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════

async def fetch_json(session, url, headers=None):
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 200:
                return await r.json(content_type=None)
            log.debug(f"HTTP {r.status} for {url}")
    except Exception as e:
        log.warning(f"Fetch error {url}: {e}")
    return None

async def update_sol_price(session):
    global SOL_PRICE_USD
    data = await fetch_json(
        session,
        "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
    )
    if data and data.get("solana", {}).get("usd"):
        SOL_PRICE_USD = float(data["solana"]["usd"])
        log.info(f"SOL price updated: ${SOL_PRICE_USD:.2f}")

async def get_anoncoin_feeds(session):
    """Fetch latest token feed from Anoncoin. Returns list of docs."""
    for url in ANONCOIN_ENDPOINTS:
        data = await fetch_json(session, url, headers=BROWSER_HEADERS)
        if not data:
            continue
        if isinstance(data, dict) and data.get("status") is True:
            docs = data.get("data", {}).get("docs", [])
            if docs:
                log.info(f"Anoncoin feed: {len(docs)} docs from {url}")
                return docs
        if isinstance(data, list) and len(data) > 0:
            log.info(f"Anoncoin feed (array): {len(data)} docs from {url}")
            return data
    log.warning("Could not fetch Anoncoin feeds from any known endpoint")
    return []

async def get_dexscreener_token(session, mint):
    """Fetch token data from DexScreener for live price/vol updates."""
    data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not data or not data.get("pairs"):
        return None
    # Return the pair with highest liquidity on Solana
    sol_pairs = [p for p in data["pairs"] if p.get("chainId") == "solana"]
    if not sol_pairs:
        return None
    return max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))


# ═══════════════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_message(doc, dex_pair=None):
    """
    Build the alert message from an Anoncoin feed doc.
    Optionally supplement with live DexScreener data.
    Format mirrors the screenshot layout.
    """
    token    = doc.get("token", {}) or {}
    user     = doc.get("userId", {}) or {}
    meta     = doc.get("metaData", {}) or {}
    trend    = doc.get("twitterTrend", {}) or {}

    # ── Token basics ──────────────────────────────────────────────
    name    = token.get("name", "Unknown")
    symbol  = token.get("symbol", "???")
    mint    = token.get("address", "")

    # ── Market data: prefer DexScreener live, fallback Anoncoin ───
    if dex_pair:
        mc_raw   = dex_pair.get("marketCap") or dex_pair.get("fdv")
        mc       = fmt_usd(mc_raw)
        price    = dex_pair.get("priceUsd", "—")
        vol_24h  = fmt_usd((dex_pair.get("volume") or {}).get("h24"))
        vol_1h   = fmt_usd((dex_pair.get("volume") or {}).get("h1"))
        vol_5m   = fmt_usd((dex_pair.get("volume") or {}).get("m5"))
        chg_24h  = fmt_pct((dex_pair.get("priceChange") or {}).get("h24"))
        holders  = fmt_num(token.get("holders", 0))
    else:
        mc       = token.get("marketCap", "—")
        price    = ""
        chg_raw  = token.get("priceChange24Hrs", "")
        chg_24h  = chg_raw if chg_raw else "—"
        vol_24h  = token.get("volume24Hrs", "—")
        vol_1h   = token.get("volume1Hrs", "—")
        vol_5m   = token.get("volume5Mins", "—")
        holders  = fmt_num(token.get("holders", 0))

    grad_pct = token.get("graduationPercentage", 0)
    tvl      = token.get("tvl", "—")

    # ── Dev / creator ─────────────────────────────────────────────
    dev_name    = user.get("name") or user.get("userName", "Unknown")
    dev_handle  = user.get("userName", "")
    twitter_obj = user.get("twitter", {}) or {}
    followers   = follower_tier(twitter_obj.get("followersFormatted", ""))

    # ── Followed by (tagged notable accounts) ────────────────────
    tagged = meta.get("tagUserProfiles") or []
    if tagged:
        followed_parts = []
        for t in tagged[:3]:
            t_name   = t.get("name") or t.get("userName", "")
            t_handle = t.get("userName", "")
            t_url    = t.get("profileURL") or f"https://x.com/{t_handle}"
            fc       = t.get("followersCount")
            if fc:
                try:
                    fc_int = int(fc)
                    if fc_int >= 1_000_000:
                        fc_str = f"{fc_int/1_000_000:.1f}M"
                    elif fc_int >= 1_000:
                        fc_str = f"{fc_int/1_000:.0f}K"
                    else:
                        fc_str = str(fc_int)
                    followed_parts.append(f"[{t_name}]({t_url}) ({fc_str})")
                except Exception:
                    followed_parts.append(f"[{t_name}]({t_url})")
            else:
                followed_parts.append(f"[{t_name}]({t_url})")
        followed_by = ", ".join(followed_parts)
    else:
        followed_by = "Not followed by anyone"

    # ── Twitter trend top voices ──────────────────────────────────
    x_views    = trend.get("xViews", 0)
    top_voices = trend.get("topVoices") or []

    # ── Launch time ───────────────────────────────────────────────
    launched_ts  = parse_iso(doc.get("addedOn", ""))
    launched_str = elapsed_str(time.time() - launched_ts) if launched_ts else "—"

    # ── Build lines ───────────────────────────────────────────────
    lines = [
        f"🌐 *New Launch*",
        SEP,
        f"🪙 *{name}* ${symbol}",
        f"👤 *Dev:* {dev_name}",
        f"👥 *Followers:* {followers}",
        f"👀 *Followed by:* {followed_by}",
    ]

    # Top voices section
    if top_voices or x_views:
        lines.append(SEP)
        if x_views:
            lines.append(f"🐦 *X Views:* {fmt_impressions(x_views)}")
        if top_voices:
            lines.append(f"📣 *Top Voices:*")
            for v in top_voices[:3]:
                v_name  = v.get("name", "")
                v_url   = v.get("tweetLink") or f"https://x.com/{v.get('username','')}"
                imp     = fmt_impressions(v.get("impressionCount", 0))
                lines.append(f"  • [{v_name}]({v_url}) — {imp} views")

    lines += [
        SEP,
        f"💰 *Market Cap:* {mc}",
        f"👥 *Holders:* {holders}",
        f"📈 *24h Change:* {chg_24h}",
        f"📊 *Vol 24h:* {vol_24h}  |  *1h:* {vol_1h}  |  *5m:* {vol_5m}",
        f"🎓 *Graduation:* {grad_pct}%",
        f"📋 *Contract:*",
        f"`{mint}`",
        SEP,
        f"🕐 Launched: {launched_str}",
        f"🔴 LIVE — updates every 30s for 1h",
    ]

    return "\n".join(lines)

def build_buttons(doc):
    token    = doc.get("token", {}) or {}
    mint     = token.get("address", "")
    meta     = doc.get("metaData", {}) or {}
    agg      = token.get("aggregators", {}) or {}

    anoncoin_url   = f"https://anoncoin.it/token/{mint}"
    dexscreener_url = agg.get("dexscreener") or f"https://dexscreener.com/solana/{mint}"
    photon_url      = agg.get("photon") or f"https://photon-sol.tinyastro.io/en/lp/{mint}"
    axiom_url       = agg.get("axiom") or f"https://axiom.trade/t/{mint}?chain=sol"

    rows = [
        [
            InlineKeyboardButton("🌐 Anoncoin",    url=anoncoin_url),
            InlineKeyboardButton("📊 DexScreener", url=dexscreener_url),
        ],
        [
            InlineKeyboardButton("⚡ Photon",      url=photon_url),
            InlineKeyboardButton("🔍 Axiom",       url=axiom_url),
        ],
    ]

    row3 = []
    twitter_link  = meta.get("twitterLink", "")
    telegram_link = meta.get("telegramLink", "")
    website_link  = meta.get("websiteLink", "")
    if twitter_link:
        row3.append(InlineKeyboardButton("🐦 Twitter", url=twitter_link))
    if telegram_link:
        row3.append(InlineKeyboardButton("✈️ Telegram", url=telegram_link))
    if website_link:
        row3.append(InlineKeyboardButton("🌍 Website", url=website_link))
    if row3:
        rows.append(row3)

    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════
# SEND & UPDATE
# ═══════════════════════════════════════════════════════════════════

async def get_token_logo(session, doc):
    """Try Anoncoin thumbnail, then DexScreener."""
    token = doc.get("token", {}) or {}
    mint  = token.get("address", "")

    media_list = doc.get("media", [])
    if media_list:
        thumb_url = media_list[0].get("thumbnailUrl", "")
        if thumb_url:
            try:
                async with session.get(thumb_url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                    if r.status == 200:
                        return await r.read()
            except Exception:
                pass

    for url in [
        f"https://dd.dexscreener.com/ds-data/tokens/solana/{mint}.png",
        f"https://img.dexscreener.com/token-images/solana/{mint}.png",
    ]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200 and "image" in r.headers.get("Content-Type", ""):
                    return await r.read()
        except Exception:
            continue
    return None

async def send_alert(bot, session, doc):
    token = doc.get("token", {}) or {}
    mint  = token.get("address", "")

    # Try to get live DexScreener data too (may not exist yet for brand new tokens)
    dex_pair = await get_dexscreener_token(session, mint)

    text    = build_message(doc, dex_pair)
    buttons = build_buttons(doc)
    logo    = await get_token_logo(session, doc)

    try:
        if logo:
            msg = await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=logo,
                caption=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=buttons,
            )
        else:
            msg = await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=buttons,
                disable_web_page_preview=True,
            )

        active_tokens[mint] = {
            "message_id":    msg.message_id,
            "chat_id":       TELEGRAM_CHAT_ID,
            "has_photo":     logo is not None,
            "alert_sent_at": time.time(),
            "doc":           doc,
        }

        sym = token.get("symbol", mint[:8])
        log.info(f"ALERT SENT: {sym} ({mint[:8]})")

    except TelegramError as e:
        log.error(f"Send failed for {mint[:8]}: {e}")

async def update_message(bot, session, mint):
    info = active_tokens.get(mint)
    if not info:
        return

    doc      = info["doc"]
    dex_pair = await get_dexscreener_token(session, mint)
    text     = build_message(doc, dex_pair)
    buttons  = build_buttons(doc)

    try:
        if info["has_photo"]:
            await bot.edit_message_caption(
                chat_id=info["chat_id"],
                message_id=info["message_id"],
                caption=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=buttons,
            )
        else:
            await bot.edit_message_text(
                chat_id=info["chat_id"],
                message_id=info["message_id"],
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=buttons,
                disable_web_page_preview=True,
            )
    except TelegramError as e:
        if "message is not modified" not in str(e).lower():
            log.warning(f"Edit failed {mint}: {e}")


# ═══════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ═══════════════════════════════════════════════════════════════════

async def scan_and_alert(bot, session):
    """Fetch Anoncoin feed and alert on any new token we haven't seen."""
    docs = await get_anoncoin_feeds(session)
    if not docs:
        return

    new_count = 0
    for doc in docs:
        token = doc.get("token", {}) or {}
        mint  = token.get("address", "")
        if not mint or mint in alerted_mints:
            continue

        alerted_mints.add(mint)
        new_count += 1
        sym = token.get("symbol", mint[:8])
        log.info(f"NEW TOKEN: {sym} ({mint[:8]})")
        await send_alert(bot, session, doc)
        await asyncio.sleep(0.5)  # small delay between sends

    if new_count:
        log.info(f"Sent {new_count} new alert(s)")

async def live_update_loop(bot, session):
    """Update active token messages every 30s for up to 1 hour."""
    sol_counter = 0
    while True:
        await asyncio.sleep(UPDATE_INTERVAL)
        sol_counter += 1
        if sol_counter >= 20:
            await update_sol_price(session)
            sol_counter = 0

        now = time.time()
        expired = [
            m for m, i in list(active_tokens.items())
            if now - i["alert_sent_at"] > UPDATE_DURATION
        ]
        for mint in expired:
            log.info(f"Expiring live updates for {mint[:8]}")
            del active_tokens[mint]

        if active_tokens:
            await asyncio.gather(
                *[update_message(bot, session, m) for m in list(active_tokens)],
                return_exceptions=True,
            )

async def debug_loop(bot, session):
    while True:
        await asyncio.sleep(300)
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"🔧 DEBUG\n"
                    f"Tokens alerted: {len(alerted_mints)}\n"
                    f"Live updates: {len(active_tokens)}\n"
                    f"SOL price: ${SOL_PRICE_USD:.2f}"
                ),
            )
        except Exception as e:
            log.error(f"Debug report failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

async def main():
    log.info("AnonCoin Launch Monitor starting...")
    log.info(f"Chat ID: {TELEGRAM_CHAT_ID}")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    async with aiohttp.ClientSession() as session:
        await update_sol_price(session)

        # Verify bot token
        try:
            me = await bot.get_me()
            log.info(f"Bot: @{me.username}")
        except TelegramError as e:
            log.error(f"Bot token error: {e}")
            return

        # Startup message
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    "🟢 AnonCoin Launch Monitor is live!\n\n"
                    "Watching: anoncoin.it bonding curve\n"
                    "Alerts on every new token launch\n"
                    "Live updates every 30s for 1 hour\n\n"
                    f"SOL: ${SOL_PRICE_USD:.2f}"
                ),
            )
        except TelegramError as e:
            log.error(f"Startup message failed: {e} — check TELEGRAM_CHAT_ID")

        # Pre-populate alerted_mints with current tokens so we don't
        # spam alerts for tokens that already exist on startup
        log.info("Loading existing tokens to avoid duplicate alerts on startup...")
        existing_docs = await get_anoncoin_feeds(session)
        for doc in existing_docs:
            mint = (doc.get("token") or {}).get("address", "")
            if mint:
                alerted_mints.add(mint)
        log.info(f"Pre-loaded {len(alerted_mints)} existing mints — will only alert on NEW tokens from now on")

        asyncio.create_task(live_update_loop(bot, session))
        if DEBUG_MODE:
            asyncio.create_task(debug_loop(bot, session))

        # Main scan loop
        while True:
            try:
                await scan_and_alert(bot, session)
            except Exception as e:
                log.error(f"Scan error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
