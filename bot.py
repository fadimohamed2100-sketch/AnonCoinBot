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

POLL_INTERVAL   = 20   # seconds between full scans
UPDATE_INTERVAL = 30   # seconds between live stat updates
UPDATE_DURATION      = 3600   # stop updating after 1 hour
MIN_LIQUIDITY_SOL    = 1.0     # minimum current liquidity in SOL
MAX_TOKEN_AGE_SECS   = 48 * 3600  # 48 hours

# Tokens we have already alerted on - never alert twice
alerted_mints: set[str] = set()

# Tokens currently being tracked for live updates
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

SOL_PRICE_USD = 140.0

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

SEP = "-" * 30


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def fmt_usd(n):
    try:
        n = float(n)
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n/1_000:.1f}K"
        return f"${n:.2f}"
    except Exception:
        return "N/A"

def fmt_sol(usd_value):
    try:
        return f"SOL {float(usd_value)/SOL_PRICE_USD:.2f}"
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

def bool_icon(v):
    return "YES" if v else "NO"


# ═══════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════

async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        log.warning(f"Fetch error {url}: {e}")
    return None

async def update_sol_price(session):
    global SOL_PRICE_USD
    data = await fetch_json(session, "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
    if data and data.get("solana", {}).get("usd"):
        SOL_PRICE_USD = float(data["solana"]["usd"])
        log.info(f"SOL price: ${SOL_PRICE_USD:.2f}")

async def get_all_pairs_for_mint(session, mint):
    data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not data or not data.get("pairs"):
        return []
    return [p for p in data["pairs"] if p.get("chainId") == "solana"]

async def get_best_monitored_pair(session, mint):
    pairs = [p for p in await get_all_pairs_for_mint(session, mint) if p.get("dexId") in MONITORED_DEXES]
    if not pairs:
        return None
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))

async def get_rugcheck(session, mint):
    return await fetch_json(session, f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")

async def get_rugcheck_full(session, mint):
    return await fetch_json(session, f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")

async def get_token_logo(session, mint):
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
# SAFETY — this is the core gate
# ═══════════════════════════════════════════════════════════════════

def check_safety(report):
    risks = {r.get("name", "").lower() for r in report.get("risks", [])}
    freeze_ok = "freeze authority enabled" not in risks
    mint_ok   = "mint authority enabled" not in risks
    # LP is safe only if it is confirmed burned OR confirmed locked
    # RugCheck adds "lp not burned" and "lp not locked" as risks when unsafe
    # So LP is safe when BOTH of those risk names are absent (meaning it IS burned or locked)
    lp_burned = "lp not burned" not in risks
    lp_locked = "lp not locked" not in risks
    lp_ok = lp_burned or lp_locked
    return (freeze_ok and mint_ok and lp_ok), {
        "freeze_disabled": freeze_ok,
        "mint_disabled":   mint_ok,
        "lp_safe":         lp_ok,
    }


# ═══════════════════════════════════════════════════════════════════
# LAUNCH TYPE
# ═══════════════════════════════════════════════════════════════════

async def detect_launch_type(session, mint, pair_info):
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
                  launch_type="direct", launch_platform="", lp_event_time=None):
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

    badge = (
        f"Graduated | {launch_platform or 'Unknown'} -> {dex_name}"
        if launch_type == "graduated"
        else f"Direct Launch | {dex_name}"
    )

    liq_change = ""
    if liq_usd and initial_liquidity:
        diff = liq_usd - initial_liquidity
        pct  = (diff / initial_liquidity) * 100
        liq_change = f" ({'UP' if diff >= 0 else 'DOWN'} {'+' if diff >= 0 else ''}{pct:.1f}%)"

    lp_str = format_timestamp(lp_event_time) if lp_event_time else "N/A"

    return "\n".join([
        f"*{name}* (${symbol})",
        f"_{badge}_",
        SEP,
        f"Token launched: {format_timestamp(launch_time)}",
        f"Price: ${price_usd}",
        f"Mkt Cap: {fmt_usd(mc)}",
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
        f"  Freeze Auth Disabled: YES",
        f"  Mint Auth Disabled:   YES",
        f"  LP Burned/Locked:     YES | {lp_str}",
        SEP,
        f"`{mint}`",
        "",
        "_Updates every 30s for 1h_",
    ])

def build_buttons(pair):
    base      = pair.get("baseToken", {})
    mint      = base.get("address", "")
    pair_addr = pair.get("pairAddress", "")
    info      = pair.get("info", {}) or {}
    socials   = {s.get("type", "").lower(): s.get("url", "") for s in (info.get("socials") or [])}
    websites  = info.get("websites") or []
    website   = websites[0].get("url", "") if websites else ""
    rows = [
        [
            InlineKeyboardButton("Photon",      url=f"https://photon-sol.tinyastro.io/en/lp/{pair_addr}"),
            InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{pair_addr}"),
        ],
        [
            InlineKeyboardButton("RugCheck",    url=f"https://rugcheck.xyz/tokens/{mint}"),
            InlineKeyboardButton("Birdeye",     url=f"https://birdeye.so/token/{mint}?chain=solana"),
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

async def send_alert(bot, session, mint, pair, safety, launch_type, launch_platform, lp_event_time):
    initial_liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    created_at  = pair.get("pairCreatedAt")
    launch_time = (created_at / 1000) if created_at else time.time()

    caption = build_caption(pair, safety, initial_liq, launch_time, launch_type, launch_platform, lp_event_time)
    buttons = build_buttons(pair)
    logo    = await get_token_logo(session, mint)

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
            "message_id": msg.message_id, "chat_id": TELEGRAM_CHAT_ID,
            "initial_liquidity": initial_liq, "launch_time": launch_time,
            "safety": safety, "has_photo": logo is not None,
            "launch_type": launch_type, "launch_platform": launch_platform,
            "lp_event_time": lp_event_time, "alert_sent_at": time.time(),
        }
        sym = pair.get("baseToken", {}).get("symbol", mint)
        log.info(f"ALERT SENT: {sym} ({mint[:8]}) | {launch_type}")
    except TelegramError as e:
        log.error(f"Send failed: {e}")

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
    )
    buttons = build_buttons(pair)
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
# CORE SCAN — this is the main change
# Scans the Photon discover endpoint directly for tokens that
# CURRENTLY pass all filters, regardless of when they launched
# ═══════════════════════════════════════════════════════════════════

async def scan_photon_discover(session):
    """
    Fetch tokens from DexScreener boosted/latest that are on our
    monitored DEXes. We check every token we haven't alerted on yet.
    This mirrors what Photon discover does — shows tokens that meet
    criteria NOW, not just when they launched.
    """
    results = []

    # Source 1: latest token profiles (new tokens)
    data = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    if data:
        results += [t.get("tokenAddress") for t in data
                    if isinstance(t, dict) and t.get("chainId") == "solana" and t.get("tokenAddress")]

    # Source 2: search each monitored DEX for recently active pairs
    for dex_id in MONITORED_DEXES:
        pairs_data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/search?q={dex_id}")
        if pairs_data and pairs_data.get("pairs"):
            for p in pairs_data["pairs"]:
                if p.get("chainId") == "solana" and p.get("dexId") == dex_id:
                    mint = (p.get("baseToken") or {}).get("address")
                    if mint:
                        results.append(mint)

    # Deduplicate
    return list(set(results))


async def process_mint(bot, session, mint):
    """
    Check a single mint. Alert if it passes all filters and we
    haven't alerted on it before.
    """
    if not mint or mint in alerted_mints:
        return

    # Must be on a monitored DEX
    pair = await get_best_monitored_pair(session, mint)
    if not pair:
        return

    # Filter 1: minimum liquidity (1 SOL minimum, hard floor)
    liq_usd = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    # Use live SOL price, but never let min drop below $10 as a safety floor
    min_liq_usd = max(MIN_LIQUIDITY_SOL * SOL_PRICE_USD, 10.0)
    if liq_usd < min_liq_usd:
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: liq too low (${liq_usd:.2f} < ${min_liq_usd:.2f})")
        return

    # Filter 2: token must be under 48 hours old - HARD REJECT if no age data
    created_at = pair.get("pairCreatedAt")
    if not created_at:
        # No creation time available - skip to be safe
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: no creation time")
        return
    age_secs = time.time() - created_at / 1000
    if age_secs > MAX_TOKEN_AGE_SECS:
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: too old ({age_secs/3600:.1f}h)")
        return
    if age_secs < 0:
        # Timestamp looks wrong
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: invalid timestamp")
        return

    # Safety check via RugCheck
    report = await get_rugcheck(session, mint)
    if not report:
        if DEBUG_MODE:
            log.info(f"SKIP {mint[:8]}: no rugcheck")
        return

    passed, safety = check_safety(report)
    if not passed:
        if DEBUG_MODE:
            risks = {r.get("name", "") for r in report.get("risks", [])}
            log.info(f"SKIP {mint[:8]}: safety failed | {risks}")
        return

    # Passed! Mark as alerted so we never double-send
    alerted_mints.add(mint)
    log.info(f"PASSES FILTERS: {mint[:8]} on {pair.get('dexId')}")

    # Gather extra info in parallel
    results = await asyncio.gather(
        detect_launch_type(session, mint, pair),
        get_lp_event_time(session, mint),
        return_exceptions=True,
    )
    launch_result, lp_event_time = results
    if isinstance(launch_result, Exception):
        launch_type, launch_platform = "direct", ""
    else:
        launch_type, launch_platform = launch_result
    if isinstance(lp_event_time, Exception):
        lp_event_time = None

    await send_alert(bot, session, mint, pair, safety, launch_type, launch_platform, lp_event_time)


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
        expired = [m for m, i in list(active_tokens.items()) if now - i["alert_sent_at"] > UPDATE_DURATION]
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
                    f"Mints checked: {len(alerted_mints)}\n"
                    f"Active alerts: {len(active_tokens)}\n"
                    f"SOL price: ${SOL_PRICE_USD:.2f}"
                ),
            )
        except Exception as e:
            log.error(f"Debug report failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

async def main():
    log.info("Solana Launch Monitor starting...")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    async with aiohttp.ClientSession() as session:
        await update_sol_price(session)

        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                parse_mode=ParseMode.MARKDOWN,
                text=(
                    "*Solana Launch Monitor is live!*\n\n"
                    "Watching: Raydium V4, CPMM, Meteora DAMM, DYN\n\n"
                    "Alerts fire when a token CURRENTLY passes:\n"
                    "  Freeze authority disabled\n"
                    "  Mint authority disabled\n"
                    "  LP burned or locked\n\n"
                    "No age limit - works like Photon discover"
                ),
            )
        except TelegramError as e:
            log.error(f"Startup message failed: {e}")

        asyncio.create_task(live_update_loop(bot, session))
        if DEBUG_MODE:
            asyncio.create_task(debug_loop(bot, session))

        while True:
            try:
                mints = await scan_photon_discover(session)
                log.info(f"Scan found {len(mints)} unique mints to check")
                # Process in small batches to avoid hammering APIs
                for i in range(0, len(mints), 5):
                    batch = mints[i:i+5]
                    await asyncio.gather(
                        *[process_mint(bot, session, m) for m in batch],
                        return_exceptions=True,
                    )
                    await asyncio.sleep(1)
            except Exception as e:
                log.error(f"Scan error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
