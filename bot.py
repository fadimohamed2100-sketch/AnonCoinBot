import os
import time
import logging
import requests
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN  = os.environ['TELEGRAM_BOT_TOKEN']
GROUP_ID   = os.environ['GROUP_ID']
TOPIC_ALL  = os.environ['TOPIC_ALL']
TOPIC_50K  = os.environ['TOPIC_50K']
TOPIC_100K = os.environ['TOPIC_100K']
TOPIC_500K = os.environ['TOPIC_500K']
TOPIC_1M   = os.environ['TOPIC_1M']
TOPIC_10M  = os.environ['TOPIC_10M']

TIERS = [
    (10_000_000, TOPIC_10M,  '🔱 10M+ Followers'),
    (1_000_000,  TOPIC_1M,   '💎 1M+ Followers'),
    (500_000,    TOPIC_500K, '🔥 500K+ Followers'),
    (100_000,    TOPIC_100K, '⚡ 100K+ Followers'),
    (50_000,     TOPIC_50K,  '🚀 50K+ Followers'),
]

FOLLOWER_MAP = {
    '10M+': 10_000_000,
    '1M+':   1_000_000,
    '100k+':   100_000,
    '10k+':     10_000,
    '1k+':       1_000,
    '0-1k':          0,
}

seen_coins = set()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Origin': 'https://anoncoin.it',
    'Referer': 'https://anoncoin.it/',
}

API_URL = 'https://api.dubdub.tv/v1/feeds'
LIVE_DURATION = 3600
LIVE_INTERVAL = 5

def parse_followers(formatted):
    return FOLLOWER_MAP.get(formatted or '0-1k', 0)

def fetch_coins():
    coins = []
    try:
        params = {'limit': 50, 'sortBy': 'added', 'chainType': 'solana'}
        r = requests.get(API_URL, headers=HEADERS, params=params, timeout=15)
        log.info(f"API status: {r.status_code}")
        if r.status_code != 200:
            log.error(f"Bad status: {r.status_code}")
            return []
        data = r.json()
        items = data.get('data', {}).get('docs', []) if isinstance(data, dict) else []
        log.info(f"Raw items from API: {len(items)}")
        for item in items:
            user          = item.get('userId') or {}
            twitter       = user.get('twitter') or {}
            followers_fmt = twitter.get('followersFormatted', '0-1k')
            followers     = parse_followers(followers_fmt)
            meta          = item.get('metaData') or {}
            notable       = []
            for nf in (meta.get('tagUserProfiles') or [])[:5]:
                n = nf.get('name') or nf.get('userName') or ''
                if n:
                    notable.append(n)
            token    = item.get('token') or {}
            contract = token.get('address') or ''
            ticker   = item.get('tickerSymbol') or token.get('symbol') or ''
            name     = item.get('title') or item.get('tickerName') or ticker or 'Unknown'
            slug     = ticker.lower() if ticker else str(item.get('_id', ''))
            media    = item.get('media') or []
            logo     = media[0].get('thumbnailUrl', '') if media else ''
            dev_name = user.get('name') or user.get('userName') or 'Unknown'
            aggs     = token.get('aggregators') or {}
            dex_url  = aggs.get('dexscreener') or f"https://dexscreener.com/search?q={contract or ticker}"
            coins.append({
                'id':                str(item.get('_id') or contract or name),
                'name':              name,
                'ticker':            ticker,
                'followers':         followers,
                'followers_fmt':     followers_fmt,
                'dev_name':          dev_name,
                'logo':              logo,
                'notable_followers': notable,
                'market_cap':        token.get('marketCap') or '—',
                'holders':           token.get('holders') or '—',
                'volume24':          token.get('volume24Hrs') or '—',
                'contract':          contract,
                'url':               f"https://anoncoin.it/coin/{slug}",
                'dex_url':           dex_url,
            })
    except Exception as e:
        log.error(f"Fetch error: {e}")
    return coins

def fetch_live_stats(contract):
    try:
        r = requests.get(f"https://api.dubdub.tv/v1/token/{contract}", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            d = r.json()
            token = d.get('data') or d.get('token') or d or {}
            if isinstance(token, dict) and 'token' in token:
                token = token['token']
            return {
                'market_cap': token.get('marketCap') or '—',
                'holders':    token.get('holders') or '—',
                'volume24':   token.get('volume24Hrs') or '—',
                'volume1h':   token.get('volume1Hrs') or '—',
                'volume5m':   token.get('volume5Mins') or '—',
                'price_chg':  token.get('priceChange24Hrs') or '—',
                'grad_pct':   token.get('graduationPercentage') or '—',
            }
    except Exception as e:
        log.debug(f"Live stats error: {e}")
    return None

TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_photo(thread_id, photo_url):
    try:
        requests.post(f"{TG_BASE}/sendPhoto", json={
            'chat_id': GROUP_ID,
            'message_thread_id': int(thread_id),
            'photo': photo_url,
        }, timeout=10)
    except Exception:
        pass

def send_text(thread_id, text):
    try:
        r = requests.post(f"{TG_BASE}/sendMessage", json={
            'chat_id': GROUP_ID,
            'message_thread_id': int(thread_id),
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=10)
        d = r.json()
        if d.get('ok'):
            return d['result']['message_id']
        log.error(f"sendMessage error: {d}")
    except Exception as e:
        log.error(f"send_text failed: {e}")
    return None

def edit_text(message_id, text):
    try:
        requests.post(f"{TG_BASE}/editMessageText", json={
            'chat_id': GROUP_ID,
            'message_id': message_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=10)
    except Exception as e:
        log.debug(f"edit_text failed: {e}")

def format_message(coin, tier_label=None, live=None):
    name          = coin['name']
    ticker        = coin['ticker']
    followers_fmt = coin['followers_fmt']
    dev_name      = coin['dev_name']
    notable       = coin['notable_followers']
    url           = coin['url']
    contract      = coin['contract']
    dex_url       = coin['dex_url']
    mkt_cap   = (live or {}).get('market_cap') or coin.get('market_cap') or '—'
    holders   = (live or {}).get('holders')    or coin.get('holders')   or '—'
    vol24     = (live or {}).get('volume24')   or coin.get('volume24')  or '—'
    vol1h     = (live or {}).get('volume1h')   or '—'
    vol5m     = (live or {}).get('volume5m')   or '—'
    price_chg = (live or {}).get('price_chg')  or '—'
    grad_pct  = (live or {}).get('grad_pct')   or '—'
    live_tag      = "\n🔴 <b>LIVE</b> — updates every 5s for 1 hour" if live is not None else ""
    header        = f"{tier_label}\n" if tier_label else "🌐 <b>New Launch</b>\n"
    ticker_line   = f" <code>${ticker}</code>" if ticker else ""
    notable_line  = f"\n👀 <b>Followed by:</b> {', '.join(notable)}" if notable else "\n👀 <b>Followed by:</b> Not followed by anyone"
    ca_line       = f"\n📋 <b>Contract:</b>\n<code>{contract}</code>" if contract else ""
    grad_line     = f"\n🎓 <b>Graduation:</b> {grad_pct}%" if grad_pct != '—' else ""
    return (
        f"{header}"
        f"━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{name}</b>{ticker_line}\n"
        f"👤 <b>Dev:</b> {dev_name}\n"
        f"👥 <b>Followers:</b> {followers_fmt}"
        f"{notable_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 <b>Market Cap:</b> {mkt_cap}\n"
        f"👤 <b>Holders:</b> {holders}\n"
        f"📈 <b>24h Change:</b> {price_chg}\n"
        f"📊 <b>Vol 24h:</b> {vol24}  |  <b>1h:</b> {vol1h}  |  <b>5m:</b> {vol5m}"
        f"{grad_line}"
        f"{ca_line}\n"
        f"━━━━━━━━━━━━━━━"
        f"{live_tag}\n"
        f"🔗 <a href='{url}'>Anoncoin</a>  |  📊 <a href='{dex_url}'>Dexscreener</a>"
    )

def live_updater(coin, message_ids, tier_label):
    contract = coin['contract']
    started  = time.time()
    log.info(f"Live updater started for {coin['name']}")
    while time.time() - started < LIVE_DURATION:
        time.sleep(LIVE_INTERVAL)
        stats = fetch_live_stats(contract)
        if not stats:
            continue
        text = format_message(coin, tier_label, live=stats)
        for mid in message_ids:
            edit_text(mid, text)
    stats = fetch_live_stats(contract) or {}
    final = format_message(coin, tier_label, live=None)
    for mid in message_ids:
        edit_text(mid, final + "\n\n✅ Live tracking ended (1 hour)")
    log.info(f"Live updater finished for {coin['name']}")

def process_coins(coins):
    new_count = 0
    for coin in coins:
        cid = coin['id']
        if not cid or cid in seen_coins:
            continue
        seen_coins.add(cid)
        new_count += 1
        followers   = coin['followers']
        logo        = coin['logo']
        message_ids = []
        tier_label  = None
        for threshold, _, label in TIERS:
            if followers >= threshold:
                tier_label = label
                break
        if logo and logo.startswith('http'):
            send_photo(TOPIC_ALL, logo)
            time.sleep(0.3)
        mid = send_text(TOPIC_ALL, format_message(coin, live={}))
        if mid:
            message_ids.append(mid)
        time.sleep(0.5)
        for threshold, topic_id, label in TIERS:
            if followers >= threshold:
                if logo and logo.startswith('http'):
                    send_photo(topic_id, logo)
                    time.sleep(0.3)
                mid = send_text(topic_id, format_message(coin, label, live={}))
                if mid:
                    message_ids.append(mid)
                time.sleep(0.5)
                break
        if message_ids and coin.get('contract'):
            t = threading.Thread(target=live_updater, args=(coin, message_ids, tier_label), daemon=True)
            t.start()
    if new_count:
        log.info(f"Posted {new_count} new coins")

def main():
    log.info("Anoncoin KOL Bot starting...")
    send_text(TOPIC_ALL, "🤖 <b>Anoncoin Monitor is live!</b>\nWatching for new launches 24/7 with live stats!")
    while True:
        try:
            log.info("Fetching coins...")
            coins = fetch_coins()
            log.info(f"Found {len(coins)} coins")
            if coins:
                process_coins(coins)
            else:
                log.warning("No coins found")
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(30)

if __name__ == '__main__':
    main()
