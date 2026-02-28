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

# Supergroup chat ID (negative number)
GROUP_ID   = int(os.getenv("GROUP_ID", "0"))

# Topic (message_thread_id) per follower tier — falls back to TOPIC_ALL if not set
TOPIC_ALL  = os.getenv("TOPIC_ALL")   # every alert goes here regardless
TOPIC_50K  = os.getenv("TOPIC_50K")   # 50k+
TOPIC_100K = os.getenv("TOPIC_100K")  # 100k+
TOPIC_500K = os.getenv("TOPIC_500K")  # 500k+
TOPIC_1M   = os.getenv("TOPIC_1M")    # 1M+
TOPIC_10M  = os.getenv("TOPIC_10M")   # 10M+

DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

POLL_INTERVAL   = 15
UPDATE_INTERVAL = 30
UPDATE_DURATION = 3600

alerted_mints: set[str] = set()
active_tokens: dict[str, dict] = {}

FEED_ENDPOINTS = [
    "https://api.dubdub.tv/v1/feeds?limit=50&sortBy=addedOn&chainType=solana",
    "https://api.dubdub.tv/v1/feeds?limit=50&sortBy=trending&chainType=solana",
    "https://api.dubdub.tv/v1/feeds?limit=50&sortBy=addedOn",
    "https://api.dubdub.tv/v1/feeds?limit=50&sortBy=trending",
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
    "Referer": "https://anoncoin.it/board",
}

# Follower tier display strings (matches anoncoin.it exactly)
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
# TOPIC ROUTING
# ═══════════════════════════════════════════════════════════════════

def get_topics_for_tier(followers_formatted: str) -> list[int | None]:
    """
    Returns a list of topic IDs to send the alert to.
    TOPIC_ALL always receives every alert.
    Higher tier topics receive alerts for their tier and above.

    Tier hierarchy:
      0-1k / 1k+ / 10k+ / 25k+  → TOPIC_ALL only
      50k+  / 250k+              → TOPIC_ALL + TOPIC_50K
      100k+                      → TOPIC_ALL + TOPIC_50K + TOPIC_100K
      500k+                      → TOPIC_ALL + TOPIC_50K + TOPIC_100K + TOPIC_500K
      1M+  / 5M+                 → TOPIC_ALL + TOPIC_50K + TOPIC_100K + TOPIC_500K + TOPIC_1M
      10M+ / 15M+                → all topics
    """
    key = (followers_formatted or "").lower().strip()

    # Build the list of topics to post to
    topics = []

    # Always add TOPIC_ALL
    if TOPIC_ALL:
        topics.append(int(TOPIC_ALL))

    if key in ("50k+", "250k+", "100k+", "500k+", "1m+", "5m+", "10m+", "15m+"):
        if TOPIC_50K:
            topics.append(int(TOPIC_50K))

    if key in ("100k+", "500k+", "1m+", "5m+", "10m+", "15m+"):
        if TOPIC_100K:
            topics.append(int(TOPIC_100K))

    if key in ("500k+", "1m+", "5m+", "10m+", "15m+"):
        if TOPIC_500K:
            topics.append(int(TOPIC_500K))

    if key in ("1m+", "5m+", "10m+", "15m+"):
        if TOPIC_1M:
            topics.append(int(TOPIC_1M))

    if key in ("10m+", "15m+"):
        if TOPIC_10M:
            topics.append(int(TOPIC_10M))

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in topics:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    # If no topics configured at all, fall back to no thread (plain group message)
    return unique if unique else [None]


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
        v = int(float(n))
        return f"{v:,}" if v else "0"
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

def parse_iso(ts_str):
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None

def follower_tier_display(followers_formatted: str) -> str:
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
        log.info(f"SOL price: ${SOL_PRICE_USD:.2f}")

async def get_feeds(session):
    seen   = set()
    result = []
    for url in FEED_ENDPOINTS:
        data = await fetch_json(session, url, headers=BROWSER_HEADERS)
        if not data:
            continue
        if isinstance(data, dict) and data.get("status") is True:
            docs = data.get("data", {}).get("docs", [])
            if docs:
                log.info(f"Feed OK ({len(docs)} docs): {url}")
                for doc in docs:
                    mint = (doc.get("token") or {}).get("address", "")
                    if mint and mint not in seen:
                        seen.add(mint)
                        result.append(doc)
                if "addedOn" in url:
                    break
            continue
        if isinstance(data, list):
            for doc in data:
                mint = (doc.get("token") or {}).get("address", "")
                if mint and mint not in seen:
                    seen.add(mint)
                    result.append(doc)
            break
    if not result:
        log.warning("All feed endpoints failed")
    return result

async def get_dexscreener_token(session, mint):
    data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not data or not data.get("pairs"):
        return None
    sol_pairs = [p for p in data["pairs"] if p.get("chainId") == "solana"]
    if not sol_pairs:
        return None
    return max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))


# ═══════════════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_message(doc, dex_pair=None):
    token = doc.get("token", {}) or {}
    user  = doc.get("userId", {}) or {}
    meta  = doc.get("metaData", {}) or {}
    trend = doc.get("twitterTrend", {}) or {}

    name   = token.get("name", "Unknown")
    symbol = token.get("symbol", "???")
    mint   = token.get("address", "")

    # Market data
    if dex_pair:
        mc_raw  = dex_pair.get("marketCap") or dex_pair.get("fdv")
        mc      = fmt_usd(mc_raw) if mc_raw else fmt_usd(token.get("marketCap"))
        chg_24h = fmt_pct((dex_pair.get("priceChange") or {}).get("h24"))
        vol_24h = fmt_usd((dex_pair.get("volume") or {}).get("h24"))
        vol_1h  = fmt_usd((dex_pair.get("volume") or {}).get("h1"))
        vol_5m  = fmt_usd((dex_pair.get("volume") or {}).get("m5"))
    else:
        mc      = token.get("marketCap", "—")
        chg_24h = token.get("priceChange24Hrs", "—") or "—"
        vol_24h = token.get("volume24Hrs", "$0") or "$0"
        vol_1h  = token.get("volume1Hrs", "—") or "—"
        vol_5m  = token.get("volume5Mins", "—") or "—"

    holders  = fmt_num(token.get("holders", 0))
    grad_pct = token.get("graduationPercentage", 0)

    # Dev info
    dev_name      = user.get("name") or user.get("userName", "Unknown")
    tw_obj        = user.get("twitter", {}) or {}
    followers_fmt = tw_obj.get("followersFormatted", "")
    followers     = follower_tier_display(followers_fmt)

    # Followed by
    tagged = meta.get("tagUserProfiles") or []
    if tagged:
        parts = []
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
                    parts.append(f"[{t_name}]({t_url}) ({fc_str})")
                except Exception:
                    parts.append(f"[{t_name}]({t_url})")
            else:
                parts.append(f"[{t_name}]({t_url})")
        followed_by = ", ".join(parts)
    else:
        followed_by = "Not followed by anyone"

    # Twitter trend
    x_views    = trend.get("xViews", 0)
    top_voices = trend.get("topVoices") or []

    # Launch time
    launched_ts  = parse_iso(doc.get("addedOn", ""))
    launched_str = elapsed_str(time.time() - launched_ts) if launched_ts else "—"

    lines = [
        f"🌐 *New Launch*",
        SEP,
        f"🪙 *{name}* ${symbol}",
        f"👤 *Dev:* {dev_name}",
        f"👥 *Followers:* {followers}",
        f"👀 *Followed by:* {followed_by}",
        SEP,
    ]

    if x_views or top_voices:
        if x_views:
            lines.append(f"🐦 *X Views:* {fmt_impressions(x_views)}")
        if top_voices:
            lines.append(f"📣 *Top Voices:*")
            for v in top_voices[:3]:
                v_name = v.get("name", "")
                v_url  = v.get("tweetLink") or f"https://x.com/{v.get('username','')}"
                imp    = fmt_impressions(v.get("impressionCount", 0))
                lines.append(f"  • [{v_name}]({v_url}) — {imp} views")
        lines.append(SEP)

    lines += [
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
    token = doc.get("token", {}) or {}
    mint  = token.get("address", "")
    meta  = doc.get("metaData", {}) or {}
    agg   = token.get("aggregators", {}) or {}

    rows = [
        [
            InlineKeyboardButton("🌐 Anoncoin",    url=f"https://anoncoin.it/token/{mint}"),
            InlineKeyboardButton("📊 DexScreener", url=agg.get("dexscreener") or f"https://dexscreener.com/solana/{mint}"),
        ],
        [
            InlineKeyboardButton("⚡ Photon",      url=agg.get("photon") or f"https://photon-sol.tinyastro.io/en/lp/{mint}"),
            InlineKeyboardButton("🔍 Axiom",       url=agg.get("axiom") or f"https://axiom.trade/t/{mint}?chain=sol"),
        ],
    ]
    row3 = []
    if meta.get("twitterLink"):
        row3.append(InlineKeyboardButton("🐦 Twitter",  url=meta["twitterLink"]))
    if meta.get("telegramLink"):
        row3.append(InlineKeyboardButton("✈️ Telegram", url=meta["telegramLink"]))
    if meta.get("websiteLink"):
        row3.append(InlineKeyboardButton("🌍 Website",  url=meta["websiteLink"]))
    if row3:
        rows.append(row3)
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════
# LOGO
# ═══════════════════════════════════════════════════════════════════

async def get_token_logo(session, doc):
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


# ═══════════════════════════════════════════════════════════════════
# SEND & UPDATE
# ═══════════════════════════════════════════════════════════════════

async def send_to_topic(bot, topic_id, text, buttons, logo):
    """Send a message to a specific topic thread."""
    kwargs = dict(
        chat_id=GROUP_ID,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons,
    )
    if topic_id is not None:
        kwargs["message_thread_id"] = topic_id

    try:
        if logo:
            msg = await bot.send_photo(photo=logo, caption=text, **kwargs)
        else:
            msg = await bot.send_message(text=text, disable_web_page_preview=True, **kwargs)
        return msg
    except TelegramError as e:
        log.error(f"Send to topic {topic_id} failed: {e}")
        return None

async def send_alert(bot, session, doc):
    token         = doc.get("token", {}) or {}
    mint          = token.get("address", "")
    user          = doc.get("userId", {}) or {}
    tw_obj        = user.get("twitter", {}) or {}
    followers_fmt = tw_obj.get("followersFormatted", "")

    dex_pair = await get_dexscreener_token(session, mint)
    text     = build_message(doc, dex_pair)
    buttons  = build_buttons(doc)
    logo     = await get_token_logo(session, doc)

    # Get all topics this follower tier should be posted to
    topics = get_topics_for_tier(followers_fmt)

    sent_messages = {}
    for topic_id in topics:
        msg = await send_to_topic(bot, topic_id, text, buttons, logo)
        if msg:
            sent_messages[topic_id] = {
                "message_id": msg.message_id,
                "has_photo":  logo is not None,
            }
        await asyncio.sleep(0.3)  # small gap between topic posts

    if sent_messages:
        active_tokens[mint] = {
            "messages":      sent_messages,   # topic_id -> {message_id, has_photo}
            "alert_sent_at": time.time(),
            "doc":           doc,
        }
        sym = token.get("symbol", mint[:8])
        log.info(f"ALERT SENT: {sym} ({mint[:8]}) | tier={followers_fmt} | topics={list(sent_messages.keys())}")

async def update_message(bot, session, mint):
    info = active_tokens.get(mint)
    if not info:
        return

    doc      = info["doc"]
    dex_pair = await get_dexscreener_token(session, mint)
    text     = build_message(doc, dex_pair)
    buttons  = build_buttons(doc)

    for topic_id, msg_info in info["messages"].items():
        try:
            if msg_info["has_photo"]:
                await bot.edit_message_caption(
                    chat_id=GROUP_ID,
                    message_id=msg_info["message_id"],
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=buttons,
                )
            else:
                await bot.edit_message_text(
                    chat_id=GROUP_ID,
                    message_id=msg_info["message_id"],
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=buttons,
                    disable_web_page_preview=True,
                )
        except TelegramError as e:
            if "message is not modified" not in str(e).lower():
                log.warning(f"Edit failed topic={topic_id} mint={mint[:8]}: {e}")
        await asyncio.sleep(0.2)


# ═══════════════════════════════════════════════════════════════════
# SCAN LOOP
# ═══════════════════════════════════════════════════════════════════

async def scan_and_alert(bot, session):
    docs = await get_feeds(session)
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
        await asyncio.sleep(0.5)
    if new_count:
        log.info(f"Sent {new_count} new alert(s)")

async def live_update_loop(bot, session):
    sol_counter = 0
    while True:
        await asyncio.sleep(UPDATE_INTERVAL)
        sol_counter += 1
        if sol_counter >= 20:
            await update_sol_price(session)
            sol_counter = 0
        now     = time.time()
        expired = [m for m, i in list(active_tokens.items()) if now - i["alert_sent_at"] > UPDATE_DURATION]
        for mint in expired:
            log.info(f"Expiring updates for {mint[:8]}")
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
                chat_id=GROUP_ID,
                message_thread_id=int(TOPIC_ALL) if TOPIC_ALL else None,
                text=(
                    f"🔧 DEBUG\n"
                    f"Tokens alerted: {len(alerted_mints)}\n"
                    f"Live updates:   {len(active_tokens)}\n"
                    f"SOL price:      ${SOL_PRICE_USD:.2f}"
                ),
            )
        except Exception as e:
            log.error(f"Debug report failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

async def main():
    log.info("AnonCoin Launch Monitor starting...")
    log.info(f"GROUP_ID:   {GROUP_ID}")
    log.info(f"TOPIC_ALL:  {TOPIC_ALL}")
    log.info(f"TOPIC_50K:  {TOPIC_50K}")
    log.info(f"TOPIC_100K: {TOPIC_100K}")
    log.info(f"TOPIC_500K: {TOPIC_500K}")
    log.info(f"TOPIC_1M:   {TOPIC_1M}")
    log.info(f"TOPIC_10M:  {TOPIC_10M}")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    async with aiohttp.ClientSession() as session:
        await update_sol_price(session)

        try:
            me = await bot.get_me()
            log.info(f"Bot: @{me.username}")
        except TelegramError as e:
            log.error(f"Bot token error: {e}")
            return

        try:
            await bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=int(TOPIC_ALL) if TOPIC_ALL else None,
                text=(
                    "🟢 AnonCoin Launch Monitor is live!\n\n"
                    "Watching: anoncoin.it new launches\n"
                    "Routing alerts by dev follower tier:\n"
                    "  TOPIC_ALL  → every launch\n"
                    "  TOPIC_50K  → 50k+ devs\n"
                    "  TOPIC_100K → 100k+ devs\n"
                    "  TOPIC_500K → 500k+ devs\n"
                    "  TOPIC_1M   → 1M+ devs\n"
                    "  TOPIC_10M  → 10M+ devs\n\n"
                    f"SOL: ${SOL_PRICE_USD:.2f}"
                ),
            )
        except TelegramError as e:
            log.error(f"Startup message failed: {e}")

        # Pre-load to avoid startup spam
        log.info("Pre-loading existing tokens...")
        existing = await get_feeds(session)
        for doc in existing:
            mint = (doc.get("token") or {}).get("address", "")
            if mint:
                alerted_mints.add(mint)
        log.info(f"Pre-loaded {len(alerted_mints)} mints — alerting only NEW tokens from now")

        asyncio.create_task(live_update_loop(bot, session))
        if DEBUG_MODE:
            asyncio.create_task(debug_loop(bot, session))

        while True:
            try:
                await scan_and_alert(bot, session)
            except Exception as e:
                log.error(f"Scan error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
