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

seen_coins = set()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Origin': 'https://anoncoin.it',
    'Referer': 'https://anoncoin.it/',
}

API_URL = 'https://api.dubdub.tv/v1/feeds'

def fetch_coins():
    coins = []
    try:
        params = {'limit': 50, 'sortBy': 'added', 'chainType': 'solana'}
        r = requests.get(API_URL, headers=HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            items = data if isinstance(data, list) else data.get('data') or data.get('items') or data.get('feeds') or data.get('tokens') or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                followers = 0
                creator = item.get('creator') or {}
                for field in ['creator_followers', 'dev_followers', 'twitter_followers', 'followers']:
                    val = item.get(field) or creator.get(field) or creator.get('twitter_followers') or 0
                    if val and isinstance(val, (int, float)) and val > 0:
                        followers = int(val)
                        break
                notable = []
                nf = item.get('notable_followers') or item.get('notableFollowers') or creator.get('notable_followers') or []
                if isinstance(nf, list):
                    for n in nf[:5]:
                        if isinstance(n, dict):
                            name = n.get('name') or n.get('username') or n.get('handle') or ''
                            if name: notable.append(name)
                        elif isinstance(n, str):
                            notable.append(n)
                contract = item.get('contract') or item.get('contract_address') or item.get('contractAddress') or item.get('mint') or item.get('address') or ''
                ticker = item.get('ticker') or item.get('symbol') or ''
                slug = item.get('slug') or str(item.get('id') or '')
                name = item.get('name') or ticker or 'Unknown'
                dev_name = item.get('creator_name') or item.get('dev_name') or creator.get('name') or creator.get('username') or creator.get('twitter_username') or 'Unknown'
                coins.append({
                    'id': str(item.get('id') or contract or slug or name),
                    'name': name,
                    'ticker': ticker,
                    'followers': followers,
                    'dev_name': dev_name,
                    'logo': item.get('image') or item.get('logo') or item.get('icon') or item.get('image_url') or '',
                    'notable_followers': notable,
                    'market_cap': str(item.get('market_cap') or item.get('marketCap') or ''),
                    'contract': contract,
                    'url': f"https://anoncoin.it/coin/{slug}" if slug else 'https://anoncoin.it/board',
                })
        else:
            log.error(f"API returned {r.status_code}")
    except Exception as e:
        log.error(f"Fetch error: {e}")
    return coins

def send_message(thread_id, text, photo_url=None):
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    payload = {'chat_id': GROUP_ID, 'message_thread_id': int(thread_id), 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    try:
        if photo_url and photo_url.startswith('http'):
            payload['photo'] = photo_url
            payload['caption'] = text
            r = requests.post(f"{base}/sendPhoto", json=payload, timeout=10)
            if not r.json().get('ok'):
                payload.pop('photo'); payload.pop('caption')
                payload['text'] = text
                requests.post(f"{base}/sendMessage", json=payload, timeout=10)
        else:
            payload['text'] = text
            requests.post(f"{base}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        log.error(f"Send failed: {e}")

def format_followers(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.0f}K"
    return str(n)

def dexscreener_url(contract, ticker):
    if contract and len(contract) > 10: return f"https://dexscreener.com/search?q={contract}"
    if ticker: return f"https://dexscreener.com/search?q={ticker}"
    return "https://dexscreener.com"

def format_message(coin, tier_label=None):
    name = coin.get('name', 'Unknown')
    ticker = coin.get('ticker', '')
    followers = coin.get('followers', 0)
    dev_name = coin.get('dev_name', 'Unknown')
    notable = coin.get('notable_followers', [])
    url = coin.get('url', 'https://anoncoin.it/board')
    mkt_cap = coin.get('market_cap', '')
    contract = coin.get('contract', '')
    header = f"{tier_label}\n" if tier_label else "🌐 <b>New Launch</b>\n"
    ticker_line = f" <code>${ticker}</code>" if ticker else ""
    notable_line = f"\n👀 <b>Followed by:</b> {', '.join(notable[:5])}" if notable else ""
    mc_line = f"\n💰 <b>Market Cap:</b> {mkt_cap}" if mkt_cap else ""
    ca_line = f"\n📋 <b>Contract:</b>\n<code>{contract}</code>" if contract else ""
    dex_url = dexscreener_url(contract, ticker)
    return (
        f"{header}━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{name}</b>{ticker_line}\n"
        f"👤 <b>Dev:</b> {dev_name}\n"
        f"👥 <b>Followers:</b> {format_followers(followers) if followers else 'Unknown'}"
        f"{notable_line}{mc_line}{ca_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔗 <a href='{url}'>Anoncoin</a>  |  📊 <a href='{dex_url}'>Dexscreener</a>"
    )

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
                log.warning("No coins found")
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(30)

if __name__ == '__main__':
    main()
