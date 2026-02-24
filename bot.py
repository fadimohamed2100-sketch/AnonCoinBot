import os
import time
import logging
import requests

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

def parse_followers(formatted):
    if not formatted:
        return 0
    return FOLLOWER_MAP.get(formatted, 0)

def fetch_coins():
    coins = []
    try:
        params = {'limit': 50, 'sortBy': 'added', 'chainType': 'solana'}
        r = requests.get(API_URL, headers=HEADERS, params=params, timeout=15)
        log.info(f"API status: {r.status_code}")
        if r.status_code != 200:
            log.error(f"Bad status: {r.status_code} - {r.text[:200]}")
            return []
        data = r.json()
        items = data.get('data', {}).get('docs', []) if isinstance(data, dict) else []
        log.info(f"Raw items from API: {len(items)}")
        for item in items:
            user = item.get('userId') or {}
            twitter = user.get('twitter') or {}
            followers_fmt = twitter.get('followersFormatted', '0-1k')
            followers = parse_followers(followers_fmt)
            meta = item.get('metaData') or {}
            notable = []
            for nf in (meta.get('tagUserProfiles') or [])[:5]:
                name = nf.get('name') or nf.get('userName') or ''
                if name:
                    notable.append(name)
            token = item.get('token') or {}
            contract = token.get('address') or ''
            ticker = item.get('tickerSymbol') or token.get('symbol') or ''
            name = item.get('title') or item.get('tickerName') or ticker or 'Unknown'
            slug = ticker.lower() if ticker else str(item.get('_id', ''))
            media = item.get('media') or []
            logo = media[0].get('thumbnailUrl', '') if media else ''
            dev_name = user.get('name') or user.get('userName') or 'Unknown'
            dex_url = (token.get('aggregators') or {}).get('dexscreener') or f"https://dexscreener.com/search?q={contract or ticker}"
            coins.append({
                'id': str(item.get('_id') or contract or name),
                'name': name,
                'ticker': ticker,
                'followers': followers,
                'followers_fmt': followers_fmt,
                'dev_name': dev_name,
                'logo': logo,
                'notable_followers': notable,
                'market_cap': token.get('marketCap') or '',
                'contract': contract,
                'url': f"https://anoncoin.it/coin/{slug}",
                'dex_url': dex_url,
            })
    except Exception as e:
        log.error(f"Fetch error: {e}")
    return coins

def send_message(thread_id, text, photo_url=None):
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    payload = {
        'chat_id': GROUP_ID,
        'message_thread_id': int(thread_id),
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    try:
        if photo_url and photo_url.startswith('http'):
            payload['photo'] = photo_url
            payload['caption'] = text
            r = requests.post(f"{base}/sendPhoto", json=payload, timeout=10)
            if not r.json().get('ok'):
                payload.pop('photo')
                payload.pop('caption')
                payload['text'] = text
                requests.post(f"{base}/sendMessage", json=payload, timeout=10)
        else:
            payload['text'] = text
            r = requests.post(f"{base}/sendMessage", json=payload, timeout=10)
            if not r.json().get('ok'):
                log.error(f"Telegram error: {r.json()}")
    except Exception as e:
        log.error(f"Send failed: {e}")

def format_message(coin, tier_label=None):
    name         = coin['name']
    ticker       = coin['ticker']
    followers_fmt = coin['followers_fmt']
    dev_name     = coin['dev_name']
    notable      = coin['notable_followers']
    url          = coin['url']
    mkt_cap      = coin['market_cap']
    contract     = coin['contract']
    dex_url      = coin['dex_url']
    header       = f"{tier_label}\n" if tier_label else "🌐 <b>New Launch</b>\n"
    ticker_line  = f" <code>${ticker}</code>" if ticker else ""
    notable_line = f"\n👀 <b>Followed by:</b> {', '.join(notable)}" if notable else ""
    mc_line      = f"\n💰 <b>Market Cap:</b> {mkt_cap}" if mkt_cap else ""
    ca_line      = f"\n📋 <b>Contract:</b>\n<code>{contract}</code>" if contract else ""
    return (
        f"{header}"
        f"━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{name}</b>{ticker_line}\n"
        f"👤 <b>Dev:</b> {dev_name}\n"
        f"👥 <b>Followers:</b> {followers_fmt}"
        f"{notable_line}"
        f"{mc_line}"
        f"{ca_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔗 <a href='{url}'>Anoncoin</a>  |  📊 <a href='{dex_url}'>Dexscreener</a>"
    )

def process_coins(coins):
    new_count = 0
    for coin in coins:
        cid = coin['id']
        if not cid or cid in seen_coins:
            continue
        seen_coins.add(cid)
        new_count += 1
        followers = coin['followers']
        logo = coin['logo']
        send_message(TOPIC_ALL, format_message(coin), logo or None)
        time.sleep(0.5)
        for threshold, topic_id, label in TIERS:
            if followers >= threshold:
                send_message(topic_id, format_message(coin, label), logo or None)
                time.sleep(0.5)
                break
    if new_count:
        log.info(f"Posted {new_count} new coins")

def main():
    log.info("Anoncoin KOL Bot starting...")
    send_message(TOPIC_ALL, "🤖 <b>Anoncoin Monitor is live!</b>\nWatching for new launches 24/7...")
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
