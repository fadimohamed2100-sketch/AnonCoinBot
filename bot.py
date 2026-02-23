import os
import re
import time
import logging
import requests
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
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

seen_coins = set()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://anoncoin.it/',
    'Origin': 'https://anoncoin.it',
}

API_ENDPOINTS = [
    'https://anoncoin.it/api/coins',
    'https://anoncoin.it/api/tokens',
    'https://anoncoin.it/api/board',
    'https://anoncoin.it/api/launches',
    'https://anoncoin.it/api/memecoins',
    'https://api.anoncoin.it/coins',
    'https://api.anoncoin.it/tokens',
    'https://api.anoncoin.it/board',
    'https://anoncoin.it/api/v1/coins',
    'https://anoncoin.it/api/v1/tokens',
    'https://anoncoin.it/api/v1/board',
]

def find_working_endpoint():
    for url in API_ENDPOINTS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data:
                    log.info(f"Found working endpoint: {url}")
                    return url
        except Exception:
            continue
    return None

def fetch_coins_from_html():
    try:
        r = requests.get('https://anoncoin.it/board', headers=HEADERS, timeout=15)
        html = r.text
        patterns = [
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
            r'window\.__DATA__\s*=\s*({.*?});',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    coins = extract_coins_from_data(data)
                    if coins:
                        log.info(f"Found {len(coins)} coins from HTML data")
                        return coins
                except Exception as e:
                    log.debug(f"Parse error: {e}")
        array_match = re.search(r'\[{"id".*?"name".*?}\]', html)
        if array_match:
            try:
                coins_raw = json.loads(array_match.group(0))
                return parse_api_coins(coins_raw)
            except Exception:
                pass
    except Exception as e:
        log.error(f"HTML fetch error: {e}")
    return []

def extract_coins_from_data(data):
    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], dict) and any(k in data[0] for k in ['name', 'symbol', 'ticker', 'id']):
            return parse_api_coins(data)
    if isinstance(data, dict):
        for key in ['coins', 'tokens', 'launches', 'items', 'data', 'results', 'memecoins']:
            if key in data:
                result = extract_coins_from_data(data[key])
                if result:
                    return result
        for val in data.values():
            if isinstance(val, (dict, list)):
                result = extract_coins_from_data(val)
                if result:
                    return result
    return []

def parse_api_coins(items):
    coins = []
    for item in items:
        if not isinstance(item, dict):
            continue
        followers = 0
        for field in ['creator_followers', 'dev_followers', 'followers', 'twitter_followers']:
            val = item.get(field)
            if val and isinstance(val, (int, float)) and val > 0:
                followers = int(val)
                break
        if followers == 0:
            creator = item.get('creator') or {}
            for field in ['followers', 'twitter_followers', 'follower_count']:
                val = creator.get(field)
                if val and isinstance(val, (int, float)) and val > 0:
                    followers = int(val)
                    break
        notable = []
        nf = item.get('notable_followers') or item.get('notableFollowers') or []
        if isinstance(nf, list):
            for n in nf[:5]:
                if isinstance(n, dict):
                    name = n.get('name') or n.get('username') or n.get('handle') or ''
                    if name:
                        notable.append(name)
                elif isinstance(n, str):
                    notable.append(n)
        contract = (
            item.get('contract') or item.get('contract_address') or
            item.get('contractAddress') or item.get('mint') or
            item.get('address') or ''
        )
        ticker = item.get('ticker') or item.get('symbol') or ''
        slug   = item.get('slug') or str(item.get('id') or '')
        name   = item.get('name') or ticker or 'Unknown'
        creator = item.get('creator') or {}
        dev_name = (
            item.get('creator_name') or item.get('dev_name') or
            creator.get('name') or creator.get('username') or
            creator.get('twitter_username') or 'Unknown'
        )
        coins.append({
            'id':                str(item.get('id') or contract or slug or name),
            'name':              name,
            'ticker':            ticker,
            'followers':         followers,
            'dev_name':          dev_name,
            'logo':              item.get('image') or item.get('logo') or item.get('icon') or item.get('image_url') or '',
            'notable_followers': notable,
            'market_cap':        str(item.get('market_cap') or item.get('marketCap') or ''),
            'contract':          contract,
            'url':               f"https://anoncoin.it/coin/{slug}" if slug else 'https://anoncoin.it/board',
        })
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
            data = r.json()
            if not data.get('ok'):
                payload.pop('photo')
                payload.pop('caption')
                payload['text'] = text
                requests.post(f"{base}/sendMessage", json=payload, timeout=10)
        else:
            payload['text'] = text
            r = requests.post(f"{base}/sendMessage", json=payload, timeout=10)
            data = r.json()
            if not data.get('ok'):
                log.error(f"Telegram error: {data}")
    except Exception as e:
        log.error(f"Send failed: {e}")

def format_followers(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.0f}K"
    return str(n)

def dexscreener_url(contract, ticker):
    if contract and len(contract) > 10:
        return f"https://dexscreener.com/search?q={contract}"
    if ticker:
        return f"https://dexscreener.com/search?q={ticker}"
    return "https://dexscreener.com"

def format_message(coin, tier_label=None):
    name      = coin.get('name', 'Unknown')
    ticker    = coin.get('ticker', '')
    followers = coin.get('followers', 0)
    dev_name  = coin.get('dev_name', 'Unknown')
    notable   = coin.get('notable_followers', [])
    url       = coin.get('url', 'https://anoncoin.it/board')
    mkt_cap   = coin.get('market_cap', '')
    contract  = coin.get('contract', '')
    header       = f"{tier_label}\n" if tier_label else "🌐 <b>New Launch</b>\n"
    ticker_line  = f" <code>${ticker}</code>" if ticker else ""
    notable_line = f"\n👀 <b>Followed by:</b> {', '.join(notable[:5])}" if notable else ""
    mc_line      = f"\n💰 <b>Market Cap:</b> {mkt_cap}" if mkt_cap else ""
    ca_line      = f"\n📋 <b>Contract:</b>\n<code>{contract}</code>" if contract else ""
    dex_url      = dexscreener_url(contract, ticker)
    return (
        f"{header}"
        f"━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{name}</b>{ticker_line}\n"
        f"👤 <b>Dev:</b> {dev_name}\n"
        f"👥 <b>Followers:</b> {format_followers(followers) if followers else 'Unknown'}"
        f"{notable_line}"
        f"{mc_line}"
        f"{ca_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔗 <a href='{url}'>Anoncoin</a>  |  📊 <a href='{dex_url}'>Dexscreener</a>"
    )

working_endpoint = None

def fetch_coins():
    global working_endpoint
    if working_endpoint:
        try:
            r = requests.get(working_endpoint, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json()
                coins = extract_coins_from_data(data) if isinstance(data, dict) else parse_api_coins(data)
                if coins:
                    return coins
        except Exception as e:
            log.debug(f"Endpoint failed: {e}")
            working_endpoint = None
    if not working_endpoint:
        working_endpoint = find_working_endpoint()
        if working_endpoint:
            try:
                r = requests.get(working_endpoint, headers=HEADERS, timeout=10)
                data = r.json()
                return extract_coins_from_data(data) if isinstance(data, dict) else parse_api_coins(data)
            except Exception:
                pass
    return fetch_coins_from_html()

def process_coins(coins):
    global seen_coins
    new_count = 0
    for coin in coins:
        cid = coin.get('id') or coin.get('name', '')
        if not cid or cid in seen_coins:
            continue
        seen_coins.add(cid)
        new_count += 1
        followers = coin.get('followers', 0)
        logo = coin.get('logo', '')
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
                log.warning("No coins found - site may be blocking requests")
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(30)

if __name__ == '__main__':
    main()
