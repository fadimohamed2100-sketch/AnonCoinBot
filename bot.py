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

POLL_INTERVAL        = 20
UPDATE_INTERVAL      = 30
UPDATE_DURATION      = 3600
MIN_LIQUIDITY_SOL    = 1.0
MAX_TOKEN_AGE_SECS   = 48 * 3600

alerted_mints: set[str] = set()
active_tokens: dict[str, dict] = {}

MONITORED_DEXES = {
    "raydium":      "Raydium V4",
    "raydium_cpmm": "Raydium CPMM",
    "meteora":      "Meteora DAMM",
    "meteora_dlmm": "Meteora DYN",
}

BONDING_CURVE_DEX_IDS = {
    "pump":     "Pump.fun",
    "pumpfun":  "Pump.fun",
    "moonshot": "Moonshot",
    "bonk":     "Bonk",
    "boop":     "Boop",
    "cyrene":   "Cyrene.ai",
    "bags":     "Bags.fm",
    "anon":     "AnonCoin.it",
}

BONDING_CURVE_DOMAINS = {
    "pump.fun":    "Pump.fun",
    "moonshot":    "Moonshot",
    "cyrene.ai":   "Cyrene.ai",
    "bags.fm":     "Bags.fm",
    "anoncoin.it": "AnonCoin.it",
    "boop.fun":    "Boop",
}

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

# Browser-like headers so the API doesn't block us
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

SOL_PRICE_USD = 140.0

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

SEP = "-" * 30


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
        return "N/A"

def fmt_sol(usd_value):
    try:
        return f"SOL {float(str(usd_value).replace('$','').replace(',',''))/SOL_PRICE_USD:.2f}"
    except Exception:
        return "N/A"

def fmt_liq(usd_value):
    return f"{fmt_sol(usd_value)} / {fmt_usd(usd_value)}"

def fmt_num(n):
    try:
        return f"{int(float(n)):,}"
    except Exception:
        return "N/A"

def elapsed_str(seconds):
    seconds = abs(int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = seconds // 60, seconds % 60
    if m < 60:
        return f"{m}m {s}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m"

def format_timestamp(ts):
    ago = elapsed_str(time.time() - ts)
    utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC")
    return f"{ago} ago ({utc})"

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


# ═══════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════

async def fetch_json(session, url, params=None, headers=None):
    try:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15)
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

async def get_anoncoin_feeds(session):
    """Try multiple Anoncoin API endpoints and return docs list."""
    for url in ANONCOIN_ENDPOINTS:
        data = await fetch_json(session, url, headers=BROWSER_HEADERS)
        if not data:
            continue
        # {"status": true, "data": {"docs": [...]}}
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

async def get_all_pairs_for_mint(session, mint):
    data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not data or not data.get("pairs"):
        return []
    return [p for p in data["pairs"] if p.get("chainId") == "solana"]

async def get_best_monitored_pair(session, mint):
    pairs = [
        p for p in await get_all_pairs_for_mint(session, mint)
        if p.get("dexId") in MONITORED_DEXES
    ]
    if not pairs:
        return None
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))

async def get_rugcheck(session, mint):
    return await fetch_json(session, f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")

async def get_rugcheck_full(session, mint):
    return await fetch_json(session, f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")

async def get_token_logo(session, mint, anoncoin_data=None):
    # Try Anoncoin thumbnail first
    if anoncoin_data:
        media_list = anoncoin_data.get("media", [])
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
        f"https://raw.githubusercontent.com/solana-labs/token-list/main/assets/mainnet/{mint}/logo.png",
    ]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200 and "image" in r.headers.get("Content-Type", ""):
                    return await r.read()
        except Exception:
            continue
    return None

async def get_lp_event_time(session, mint):
    report = await get_rugcheck_full(session, mint)
    if not report:
        return None
    try:
        for market in (report.get("markets") or []):
            lp = market.get("lp") or {}
            t = lp.get("burnedAt") or lp.get("lpBurnedAt")
            if t:
                return float(t)
            lock = lp.get("lockInfo") or {}
            t = lock.get("lockedAt") or lock.get("createdAt")
            if t:
                return float(t)
        t = report.get("lpBurnedAt") or report.get("burnedAt")
        if t:
            return float(t)
    except Exception as e:
        log.warning(f"LP time error {mint}: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════
# SAFETY
# ═══════════════════════════════════════════════════════════════════

def check_safety(report):
    risks = {r.get("name", "").lower() for r in report.get("risks", [])}
    freeze_ok = "freeze authority enabled" not in risks
    mint_ok   = "mint authority enabled" not in risks
    lp_ok     = ("lp not burned" not in risks) or ("lp not locked" not in risks)
    return (freeze_ok and mint_ok and lp_ok), {
        "freeze_disabled": freeze_ok,
        "mint_disabled":   mint_ok,
        "lp_safe":         lp_ok,
    }


# ═══════════════════════════════════════════════════════════════════
# LAUNCH TYPE
# ═══════════════════════════════════════════════════════════════════

async def detect_launch_type(session, mint, pair_info, anoncoin_data=None):
    if anoncoin_data:
        return "graduated", "AnonCoin.it"
    all_pairs = await get_all_pairs_for_mint(session, mint)
    if all_pairs:
        for p in all_pairs:
            dex_id = p.get("dexId", "").lower()
            for keyword, name in BONDING_CURVE_DEX_IDS.items():
                if keyword in dex_id:
                    return "graduated", name
    info = pair_info.get("info", {}) or {}
    all_links = (
        [s.get("url", "") for s in (info.get("socials") or [])] +
        [w.get("url", "") for w in (info.get("websites") or [])]
    )
    for link in all_links:
        for domain, name in BONDING_CURVE_DOMAINS.items():
            if domain in link.lower():
                return "graduated", name
    if all_pairs:
        dex_ids = {p.get("dexId", "") for p in all_pairs}
        if (dex_ids & set(MONITORED_DEXES.keys())) and (dex_ids - set(MONITORED_DEXES.keys())):
            return "graduated", "Unknown"
    return "direct", ""


# ═══════════════════════════════════════════════════════════════════
# MESSAGE
# ═══════════════════════════════════════════════════════════════════

def build_caption(pair, safety, initial_liquidity, launch_time,
                  launch_type="direct", launch_platform="", lp_event_time=None,
                  anoncoin_data=None):
    base      = pair.get("baseToken", {})
    name      = base.get("name", "Unknown")
    symbol    = base.get("symbol", "???")
    mint      = base.get("address", "")
    dex_name  = MONITORED_DEXES.get(pair.get("dexId", ""), pair.get("dexId", "").title())
    price_usd = pair.get("priceUsd", "N/A")
    mc        = pair.get("marketCap") or pair.get("fdv")
    liq_usd   = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    vol_5m    = (pair.get("volume") or {}).get("m5")
    vol_1h    = (pair.get("volume") or {}).get("h1")
    buys_5m   = (pair.get("txns") or {}).get("m5", {}).get("buys", 0)
    sells_5m  = (pair.get("txns") or {}).get("m5", {}).get("sells", 0)
    buys_1h   = (pair.get("txns") or {}).get("h1", {}).get("buys", 0)
    sells_1h  = (pair.get("txns") or {}).get("h1", {}).get("sells", 0)
    grad_pct  = None
    holders   = "N/A"

    if anoncoin_data:
        token = anoncoin_data.get("token", {})
        if not mc:
            mc = parse_usd_str(token.get("marketCap", "0")) or None
        if not price_usd or price_usd == "N/A":
            p_raw = token.get("price", {})
            price_usd = str(p_raw.get("$numberDecimal", "N/A")) if isinstance(p_raw, dict) else str(p_raw or "N/A")
        if not liq_usd:
            liq_usd = parse_usd_str(token.get("tvl", "0"))
        if not vol_5m:
            vol_5m = token.get("volume5Mins")
        if not vol_1h:
            vol_1h = token.get("volume1Hrs")
        grad_pct = token.get("graduationPercentage")
        holders  = token.get("holders", "N/A")

    badge = (
        f"Graduated | {launch_platform or 'AnonCoin.it'} -> {dex_name}"
        if launch_type == "graduated"
        else f"Direct Launch | {dex_name}"
    )

    liq_change = ""
    if liq_usd and initial_liquidity:
        diff = liq_usd - initial_liquidity
        pct  = (diff / initial_liquidity) * 100
        liq_change = f" ({'UP' if diff >= 0 else 'DOWN'} {'+' if diff >= 0 else ''}{pct:.1f}%)"

    lp_str = format_timestamp(lp_event_time) if lp_event_time else "N/A"

    lines = [
        f"*{name}* (${symbol})",
        f"_{badge}_",
        SEP,
        f"Token launched: {format_timestamp(launch_time)}",
        f"Price: ${price_usd}",
        f"Mkt Cap: {fmt_usd(mc)}",
    ]
    if grad_pct is not None:
        lines.append(f"Graduation: {grad_pct}% | Holders: {holders}")
    lines += [
        SEP,
        "*Liquidity*",
        f"  Launch:   {fmt_liq(initial_liquidity)}",
        f"  Current:  {fmt_liq(liq_usd)}{liq_change}",
        SEP,
        "*Volume*",
        f"  5m: {fmt_usd(vol_5m)}   1h: {fmt_usd(vol_1h)}",
        SEP,
        "*Transactions*",
        f"  5m: {fmt_num(buys_5m)} buys / {fmt_num(sells_5m)} sells",
        f"  1h: {fmt_num(buys_1h)} buys / {fmt_num(sells_1h)} sells",
        SEP,
        "*Safety* (all passed)",
        "  Freeze Auth Disabled: YES",
        "  Mint Auth Disabled:   YES",
        f"  LP Burned/Locked:     YES | {lp_str}",
        SEP,
        f"`{mint}`",
        "",
        "_Updates every 30s for 1h_",
    ]
    return "\n".join(lines)

def build_buttons(pair, anoncoin_data=None):
    base      = pair.get("baseToken", {})
    mint      = base.get("address", "")
    pair_addr = pair.get("pairAddress", "")
    info      = pair.get("info", {}) or {}
    socials   = {s.get("type", "").lower(): s.get("url", "") for s in (info.get("socials") or [])}
    websites  = info.get("websites") or []
    website   = websites[0].get("url", "") if websites else ""

    if anoncoin_data:
        meta = anoncoin_data.get("metaData", {}) or {}
        if not socials.get("twitter") and meta.get("twitterLink"):
            socials["twitter"] = meta["twitterLink"]
        if not socials.get("telegram") and meta.get("telegramLink"):
            socials["telegram"] = meta["telegramLink"]
        if not website and meta.get("websiteLink"):
            website = meta["websiteLink"]

    target = pair_addr or mint
    rows = [
        [
            InlineKeyboardButton("Photon",      url=f"https://photon-sol.tinyastro.io/en/lp/{target}"),
            InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{target}"),
        ],
        [
            InlineKeyboardButton("RugCheck",    url=f"https://rugcheck.xyz/tokens/{mint}"),
            InlineKeyboardButton("Birdeye",     url=f"https://birdeye.so/token/{mint}?chain=solana"),
        ],
        [
            InlineKeyboardButton("AnonCoin.it", url=f"https://anoncoin.it/token/{mint}"),
        ],
    ]
    row3 = []
    if website:
        row3.append(InlineKeyboardButton("Website",  url=website))
    if socials.get("twitter"):
        row3.append(InlineKeyboardButton("Twitter",  url=socials["twitter"]))
    if socials.get("telegram"):
        row3.append(InlineKeyboardButton("Telegram", url=socials["telegram"]))
    if row3:
        rows.append(row3)
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════
# SEND & UPDATE
# ═══════════════════════════════════════════════════════════════════

async def send_alert(bot, session, mint, pair, safety, launch_type, launch_platform,
                     lp_event_time, anoncoin_data=None):
    liq_dex = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    if not liq_dex and anoncoin_data:
        liq_dex = parse_usd_str(anoncoin_data.get("token", {}).get("tvl", "0"))
    initial_liq = liq_dex

    created_at = pair.get("pairCreatedAt")
    if created_at:
        launch_time = float(created_at) / 1000
    elif anoncoin_data:
        launch_time = parse_iso(anoncoin_data.get("addedOn", "")) or time.time()
    else:
        launch_time = time.time()

    caption = build_caption(pair, safety, initial_liq, launch_time,
                            launch_type, launch_platform, lp_event_time, anoncoin_data)
    buttons = build_buttons(pair, anoncoin_data)
    logo    = await get_token_logo(session, mint, anoncoin_data)

    try:
        if logo:
            msg = await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID, photo=logo,
                caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons,
            )
        else:
            msg = await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=buttons,
                disable_web_page_preview=True,
            )
        active_tokens[mint] = {
            "message_id":        msg.message_id,
            "chat_id":           TELEGRAM_CHAT_ID,
            "initial_liquidity": initial_liq,
            "launch_time":       launch_time,
            "safety":            safety,
            "has_photo":         logo is not None,
            "launch_type":       launch_type,
            "launch_platform":   launch_platform,
            "lp_event_time":     lp_event_time,
            "alert_sent_at":     time.time(),
            "anoncoin_data":     anoncoin_data,
        }
        sym = (pair.get("baseToken", {}).get("symbol")
               or (anoncoin_data.get("token", {}).get("symbol") if anoncoin_data else None)
               or mint[:8])
        log.info(f"ALERT SENT: {sym} ({mint[:8]}) | {launch_type} | {launch_platform}")
    except TelegramError as e:
        log.error(f"Send failed for {mint[:8]}: {e}")

async def update_message(bot, session, mint):
    info = active_tokens.get(mint)
    if not info:
        return
    pair = await get_best_monitored_pair(session, mint)
    if not pair:
        return
    caption = build_caption(
        pair, info["safety"], info["initial_liquidity"], info["launch_time"],
        info["launch_type"], info["launch_platform"], info["lp_event_time"],
        info.get("anoncoin_data"),
    )
    buttons = build_buttons(pair, info.get("anoncoin_data"))
    try:
        if info["has_photo"]:
            await bot.edit_message_caption(
                chat_id=info["chat_id"], message_id=info["message_id"],
                caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons,
            )
        else:
            await bot.edit_message_text(
                chat_id=info["chat_id"], message_id=info["message_id"],
                text=caption, parse_mode=ParseMode.MARKDOWN,
                reply_markup=buttons, disable_web_page_preview=True,
            )
    except TelegramError as e:
        if "message is not modified" not in str(e).lower():
            log.warning(f"Edit failed {mint}: {e}")


# ═══════════════════════════════════════════════════════════════════
# CORE SCAN
# ═══════════════════════════════════════════════════════════════════

async def scan_sources(session):
    results = {}  # mint -> anoncoin_doc

    # Primary: Anoncoin feeds API
    docs = await get_anoncoin_feeds(session)
    for doc in docs:
        token = doc.get("token", {})
        mint = token.get("address", "")
        if mint:
            results[mint] = doc

    # Secondary: DexScreener latest profiles
    data = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    if data and isinstance(data, list):
        for t in data:
            if isinstance(t, dict) and t.get("chainId") == "solana":
                mint = t.get("tokenAddress", "")
                if mint and mint not in results:
                    results[mint] = None

    # Secondary: DexScreener DEX search
    for dex_id in MONITORED_DEXES:
        pairs_data = await fetch_json(
            session, f"https://api.dexscreener.com/latest/dex/search?q={dex_id}"
        )
        if pairs_data and pairs_data.get("pairs"):
            for p in pairs_data["pairs"]:
                if p.get("chainId") == "solana" and p.get("dexId") == dex_id:
                    mint = (p.get("baseToken") or {}).get("address")
                    if mint and mint not in results:
                        results[mint] = None

    return list(results.items())


async def process_mint(bot, session, mint, anoncoin_data=None):
    if not mint or mint in alerted_mints:
        return

    pair = await get_best_monitored_pair(session, mint)
    if not pair:
        if DEBUG_MODE:
            sym = (anoncoin_data or {}).get("token", {}).get("symbol", mint[:8])
            grad = (anoncoin_data or {}).get("token", {}).get("graduationPercentage", "?")
            log.info(f"SKIP {sym}: not on monitored DEX yet (grad={grad}%)")
        return

    # Skip Pump.fun -> Meteora
    all_pairs_check = await get_all_pairs_for_mint(session, mint)
    dex_ids_check = {p.get("dexId", "").lower() for p in all_pairs_check}
    if any("pump" in d for d in dex_ids_check) and any("meteora" in d for d in dex_ids_check):
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: Pump.fun -> Meteora graduation")
        return

    # Liquidity filter
    liq_usd = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    if not liq_usd and anoncoin_data:
        liq_usd = parse_usd_str((anoncoin_data.get("token") or {}).get("tvl", "0"))
    min_liq_usd = max(MIN_LIQUIDITY_SOL * SOL_PRICE_USD, 10.0)
    if liq_usd < min_liq_usd:
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: liq too low (${liq_usd:.2f} < ${min_liq_usd:.2f})")
        return

    # Age filter
    launch_time_for_age = None
    created_at = pair.get("pairCreatedAt")
    if created_at:
        try:
            launch_time_for_age = float(created_at) / 1000.0
        except (TypeError, ValueError):
            pass
    if not launch_time_for_age and anoncoin_data:
        launch_time_for_age = parse_iso(anoncoin_data.get("addedOn", ""))

    if launch_time_for_age:
        age_secs = time.time() - launch_time_for_age
        if age_secs < 0 or age_secs > MAX_TOKEN_AGE_SECS:
            if DEBUG_MODE:
                log.info(f"SKIP {mint[:8]}: age={age_secs/3600:.1f}h")
            return
    else:
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: no valid creation time")
        return

    # RugCheck safety
    report = await get_rugcheck(session, mint)
    if not report:
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: no rugcheck response")
        return

    passed, safety = check_safety(report)
    if not passed:
        if DEBUG_MODE:
            risks = {r.get("name", "") for r in report.get("risks", [])}
            log.info(f"SKIP {mint[:8]}: safety failed | {risks}")
        return

    alerted_mints.add(mint)
    sym = (pair.get("baseToken", {}).get("symbol")
           or (anoncoin_data or {}).get("token", {}).get("symbol")
           or mint[:8])
    log.info(f"PASSES FILTERS: {sym} ({mint[:8]}) on {pair.get('dexId')}")

    results = await asyncio.gather(
        detect_launch_type(session, mint, pair, anoncoin_data),
        get_lp_event_time(session, mint),
        return_exceptions=True,
    )
    launch_result, lp_event_time = results
    launch_type, launch_platform = (
        launch_result if not isinstance(launch_result, Exception) else ("direct", "")
    )
    if isinstance(lp_event_time, Exception):
        lp_event_time = None

    await send_alert(bot, session, mint, pair, safety, launch_type, launch_platform,
                     lp_event_time, anoncoin_data)


# ═══════════════════════════════════════════════════════════════════
# BACKGROUND LOOPS
# ═══════════════════════════════════════════════════════════════════

async def live_update_loop(bot, session):
    counter = 0
    while True:
        await asyncio.sleep(UPDATE_INTERVAL)
        counter += 1
        if counter >= 20:
            await update_sol_price(session)
            counter = 0
        now = time.time()
        expired = [
            m for m, i in list(active_tokens.items())
            if now - i["alert_sent_at"] > UPDATE_DURATION
        ]
        for mint in expired:
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
                    f"DEBUG REPORT\n"
                    f"Mints alerted: {len(alerted_mints)}\n"
                    f"Active live alerts: {len(active_tokens)}\n"
                    f"SOL price: ${SOL_PRICE_USD:.2f}"
                ),
            )
        except Exception as e:
            log.error(f"Debug report failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

async def main():
    log.info("Solana Launch Monitor (Anoncoin edition) starting...")
    log.info(f"Chat ID configured: {TELEGRAM_CHAT_ID}")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    async with aiohttp.ClientSession() as session:
        await update_sol_price(session)

        # Verify bot token
        try:
            me = await bot.get_me()
            log.info(f"Bot connected: @{me.username}")
        except TelegramError as e:
            log.error(f"Bot token invalid or unreachable: {e}")
            return

        # Send startup message (non-fatal if it fails)
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                parse_mode=ParseMode.MARKDOWN,
                text=(
                    "*Solana Launch Monitor is live!*\n\n"
                    "Sources: AnonCoin\\.it \\+ DexScreener\n"
                    "Watching: Raydium V4, CPMM, Meteora DAMM, DYN\n\n"
                    "Filters:\n"
                    "  Freeze authority disabled\n"
                    "  Mint authority disabled\n"
                    "  LP burned or locked\n\n"
                    "Max token age: 48 hours\n"
                    "Min liquidity: 1 SOL"
                ),
            )
        except TelegramError as e:
            log.error(f"Startup message failed: {e}")
            log.error(">>> Check TELEGRAM_CHAT_ID environment variable on Railway <<<")

        asyncio.create_task(live_update_loop(bot, session))
        if DEBUG_MODE:
            asyncio.create_task(debug_loop(bot, session))

        while True:
            try:
                mint_pairs = await scan_sources(session)
                log.info(f"Scan found {len(mint_pairs)} unique mints to check")
                for i in range(0, len(mint_pairs), 5):
                    batch = mint_pairs[i:i+5]
                    await asyncio.gather(
                        *[process_mint(bot, session, mint, anon_doc)
                          for mint, anon_doc in batch],
                        return_exceptions=True,
                    )
                    await asyncio.sleep(1)
            except Exception as e:
                log.error(f"Scan error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
