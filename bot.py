import os
import re
import time
import logging
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

# ── Telegram ───────────────────────────────────────────────────────────────────
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
        else:
            payload['text'] = text
            r = requests.post(f"{base}/sendMessage", json=payload, timeout=10)
        data = r.json()
        if not data.get('ok'):
            # Photo failed — retry as text only
            payload.pop('photo', None)
            payload.pop('caption', None)
            payload['text'] = text
            payload['disable_web_page_preview'] = True
            r = requests.post(f"{base}/sendMessage", json=payload, timeout=10)
            data = r.json()
            if not data.get('ok'):
                log.error(f"Telegram error: {data}")
    except Exception as e:
        log.error(f"Send failed: {e}")

def format_followers(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)

def dexscreener_url(contract, ticker):
    """Build a dexscreener search link for the token."""
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

    # Contract address — in code block for easy tap-to-copy
    if contract:
        ca_line = f"\n📋 <b>Contract:</b>\n<code>{contract}</code>"
    else:
        ca_line = ""

    dex_url = dexscreener_url(contract, ticker)

    return (
        f"{header}"
        f"━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{name}</b>{ticker_line}\n"
        f"👤 <b>Dev:</b> {dev_name}\n"
        f"👥 <b>Followers:</b> {format_followers(followers)}"
        f"{notable_line}"
        f"{mc_line}"
        f"{ca_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔗 <a href='{url}'>Anoncoin</a>  |  "
        f"📊 <a href='{dex_url}'>Dexscreener</a>"
    )

# ── Scraper ────────────────────────────────────────────────────────────────────
def make_driver():
    opts = Options()
    opts.add_argument('--headless')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1280,800')
    opts.add_argument('--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36')
    return webdriver.Chrome(options=opts)

def scrape_coins(driver):
    coins = []
    try:
        driver.get('https://anoncoin.it/board')
        time.sleep(5)

        # Try Next.js page data first
        raw = driver.execute_script("return window.__NEXT_DATA__ || null;")
        if raw and isinstance(raw, dict):
            try:
                props = raw.get('props', {}).get('pageProps', {})
                for key in ['coins', 'tokens', 'launches', 'items']:
                    items = props.get(key, [])
                    if items:
                        return parse_api_coins(items)
            except Exception:
                pass

        # DOM fallback
        selectors = [
            '[class*="coin-card"]', '[class*="token-card"]',
            '[class*="launch-card"]', '[class*="CoinCard"]',
            '[class*="TokenCard"]', '[class*="card"]'
        ]
        cards = []
        for sel in selectors:
            found = driver.find_elements(By.CSS_SELECTOR, sel)
            if len(found) > 2:
                cards = found
                break

        for card in cards[:50]:
            try:
                coin = parse_card(card)
                if coin:
                    coins.append(coin)
            except Exception as e:
                log.debug(f"Card error: {e}")

    except Exception as e:
        log.error(f"Scrape error: {e}")

    return coins

def parse_api_coins(items):
    coins = []
    for item in items:
        followers = (
            item.get('creator_followers') or
            item.get('dev_followers') or
            item.get('followers') or
            item.get('twitter_followers') or
            (item.get('creator') or {}).get('followers') or
            (item.get('creator') or {}).get('twitter_followers') or 0
        )
        notable = []
        nf = item.get('notable_followers') or item.get('notableFollowers') or []
        if isinstance(nf, list):
            notable = [n.get('name') or n.get('username') or str(n) for n in nf[:5]]

        contract = (
            item.get('contract') or item.get('contract_address') or
            item.get('contractAddress') or item.get('address') or ''
        )
        ticker = item.get('ticker') or item.get('symbol') or ''
        slug   = item.get('slug') or item.get('id') or ''

        coins.append({
            'id':               str(item.get('id') or contract or slug or item.get('name', '')),
            'name':             item.get('name') or item.get('symbol') or 'Unknown',
            'ticker':           ticker,
            'followers':        followers,
            'dev_name': (
                item.get('creator_name') or item.get('dev_name') or
                (item.get('creator') or {}).get('name') or
                (item.get('creator') or {}).get('username') or 'Unknown'
            ),
            'logo':             item.get('image') or item.get('logo') or item.get('icon') or '',
            'notable_followers': notable,
            'market_cap':       item.get('market_cap') or item.get('marketCap') or '',
            'contract':         contract,
            'url':              f"https://anoncoin.it/coin/{slug}",
        })
    return coins

# Solana/EVM contract address patterns
CA_PATTERN = re.compile(
    r'\b([1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40})\b'
)

def parse_card(card):
    text = card.text or ''
    if not text.strip():
        return None

    # Followers
    fm = re.search(r'([\d,.]+)\s*([KMBkmb])?\s*[Ff]ollower', text)
    followers = 0
    if fm:
        num  = float(fm.group(1).replace(',', ''))
        mult = (fm.group(2) or '').upper()
        if mult == 'K':   followers = int(num * 1_000)
        elif mult == 'M': followers = int(num * 1_000_000)
        elif mult == 'B': followers = int(num * 1_000_000_000)
        else:             followers = int(num)

    # Ticker
    tm = re.search(r'\$([A-Z]{2,10})', text)
    ticker = tm.group(1) if tm else ''

    # Name
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    name  = lines[0] if lines else 'Unknown'

    # Contract address
    ca_match = CA_PATTERN.search(text)
    contract = ca_match.group(1) if ca_match else ''

    # Also check data attributes
    try:
        contract = contract or card.get_attribute('data-contract') or card.get_attribute('data-address') or ''
    except Exception:
        pass

    # Logo
    logo = ''
    try:
        img  = card.find_element(By.TAG_NAME, 'img')
        logo = img.get_attribute('src') or ''
    except Exception:
        pass

    # URL
    url = 'https://anoncoin.it/board'
    try:
        link = card.find_element(By.TAG_NAME, 'a')
        url  = link.get_attribute('href') or url
    except Exception:
        pass

    # Notable followers
    notable = re.findall(r'@([\w]+)', text)[:5]

    # Market cap
    mcm     = re.search(r'\$?([\d,.]+\s*[KMBkmb]?)\s*(?:market\s*cap|mcap|mc\b)', text, re.I)
    mkt_cap = mcm.group(0).strip() if mcm else ''

    coin_id = contract or url if url != 'https://anoncoin.it/board' else f"{name}-{ticker}"

    return {
        'id':               coin_id,
        'name':             name,
        'ticker':           ticker,
        'followers':        followers,
        'dev_name':         'Unknown',
        'logo':             logo,
        'notable_followers': notable,
        'market_cap':       mkt_cap,
        'contract':         contract,
        'url':              url,
    }

# ── Main loop ──────────────────────────────────────────────────────────────────
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
        logo      = coin.get('logo', '')

        # Post to All Launches topic
        send_message(TOPIC_ALL, format_message(coin), logo or None)
        time.sleep(0.5)

        # Post to highest matching tier
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

    driver = make_driver()
    try:
        while True:
            try:
                log.info("Scraping Anoncoin...")
                coins = scrape_coins(driver)
                log.info(f"Found {len(coins)} coins on page")
                process_coins(coins)
            except Exception as e:
                log.error(f"Loop error: {e}")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(15)
                driver = make_driver()
            time.sleep(30)
    finally:
        driver.quit()

if __name__ == '__main__':
    main()
