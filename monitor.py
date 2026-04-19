import requests
import time
import asyncio
import json
import re
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import config

# ─── VOLUME HISTORY TRACKER ──────────────────────────────────
# Simpan snapshot volume per token untuk deteksi resurrection
volume_history = {}  # address -> list of (timestamp, volume_1h)
seen_tokens = set()


def track_volume(address, volume_1h):
    """Simpan history volume untuk deteksi resurrection"""
    now = time.time()
    if address not in volume_history:
        volume_history[address] = []
    volume_history[address].append((now, volume_1h))
    # Simpan max 10 snapshot terakhir
    volume_history[address] = volume_history[address][-10:]


def detect_resurrection(address, current_volume_1h):
    """
    Deteksi volume resurrection:
    Token yang sebelumnya volume rendah/mati tiba-tiba spike
    Return: (is_resurrection, multiplier)
    """
    if address not in volume_history or len(volume_history[address]) < 2:
        return False, 0

    history = volume_history[address]
    # Ambil rata-rata volume 1h sebelumnya (exclude yang terbaru)
    prev_volumes = [v for _, v in history[:-1]]
    avg_prev = sum(prev_volumes) / len(prev_volumes)

    if avg_prev < 100:  # Sebelumnya hampir mati (< $100/jam)
        if current_volume_1h > 5000:  # Tiba-tiba > $5k/jam
            multiplier = current_volume_1h / max(avg_prev, 1)
            return True, round(multiplier, 0)

    return False, 0


# ─── NITTER / TWITTER SCRAPER ────────────────────────────────

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.lucahammer.com",
]

def search_twitter_mentions(query, max_results=5):
    """
    Cari mention di Twitter via Nitter scraping.
    query bisa berupa contract address atau nama token.
    Return: list of tweet snippets
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    tweets = []

    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/search?q={requests.utils.quote(query)}&f=tweets"
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue

            content = r.text

            # Extract tweet content dengan regex sederhana
            # Nitter HTML: <div class="tweet-content media-body">...</div>
            pattern = r'class="tweet-content[^"]*"[^>]*>(.*?)</div>'
            matches = re.findall(pattern, content, re.DOTALL)

            for m in matches[:max_results]:
                # Bersihkan HTML tags
                clean = re.sub(r'<[^>]+>', '', m).strip()
                clean = re.sub(r'\s+', ' ', clean)
                if len(clean) > 10:
                    tweets.append(clean)

            if tweets:
                break  # Berhasil dari instance ini

        except Exception as e:
            print(f"  Nitter {instance} error: {e}")
            continue

    return tweets


def analyze_twitter_signal(token_name, token_symbol, token_address):
    """
    Analisa Twitter signal untuk sebuah token.
    Return: (score_bonus, twitter_summary)
    """
    bonus = 0
    mentions = []

    # Search by symbol (lebih sering disebut di Twitter)
    symbol_query = f"${token_symbol} solana"
    tweets_symbol = search_twitter_mentions(symbol_query, max_results=5)

    # Search by contract address (paling akurat)
    addr_tweets = search_twitter_mentions(token_address[:20], max_results=3)

    all_tweets = tweets_symbol + addr_tweets
    tweet_count = len(all_tweets)

    if tweet_count == 0:
        return 0, "❌ Tidak ada mention di Twitter"

    # Hitung sentiment sederhana
    positive_words = ["moon", "gem", "100x", "1000x", "buy", "bullish", "pump",
                      "launch", "new", "early", "alpha", "call", "fire", "🔥",
                      "🚀", "💎", "ape", "send", "go", "legit", "safu"]
    negative_words = ["rug", "scam", "dump", "sell", "rekt", "dead", "fake",
                      "honeypot", "avoid", "warning", "careful", "sus"]

    pos_count = 0
    neg_count = 0

    for tweet in all_tweets:
        tweet_lower = tweet.lower()
        for w in positive_words:
            if w in tweet_lower:
                pos_count += 1
        for w in negative_words:
            if w in tweet_lower:
                neg_count += 1

    # Scoring
    if tweet_count >= 5:
        bonus += 10
    elif tweet_count >= 2:
        bonus += 5

    if neg_count > pos_count:
        bonus -= 10
        sentiment = "🔴 Sentiment negatif"
    elif pos_count > neg_count * 2:
        bonus += 10
        sentiment = "🟢 Sentiment positif"
    else:
        sentiment = "🟡 Sentiment netral"

    summary = f"📱 Twitter: {tweet_count} mention | {sentiment}"
    return bonus, summary


# ─── DATA FETCHING ───────────────────────────────────────────

def get_solana_pairs():
    """Ambil token Solana dari DexScreener - multiple endpoints"""
    all_pairs = []

    # Endpoint 1: Token profiles (biasanya pump.fun graduates)
    try:
        r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        if r.status_code == 200:
            sol_tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Profiles: {len(sol_tokens)}")
            for token in sol_tokens[:25]:
                addr = token.get("tokenAddress", "")
                if not addr:
                    continue
                try:
                    rp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
                    if rp.status_code == 200:
                        pairs = rp.json().get("pairs", [])
                        if pairs:
                            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                            all_pairs.append(best)
                    time.sleep(0.15)
                except:
                    continue
    except Exception as e:
        print(f"  Profiles error: {e}")

    # Endpoint 2: Boosted tokens
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        if r.status_code == 200:
            sol_boosted = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Boosted: {len(sol_boosted)}")
            existing = {p.get("baseToken", {}).get("address") for p in all_pairs}
            for token in sol_boosted[:20]:
                addr = token.get("tokenAddress", "")
                if not addr or addr in existing:
                    continue
                try:
                    rp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
                    if rp.status_code == 200:
                        pairs = rp.json().get("pairs", [])
                        if pairs:
                            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                            all_pairs.append(best)
                            existing.add(addr)
                    time.sleep(0.15)
                except:
                    continue
    except Exception as e:
        print(f"  Boosted error: {e}")

    # Endpoint 3: New pairs Solana (tangkap yang baru graduate)
    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/search?q=pump+sol", timeout=10)
        if r.status_code == 200:
            pairs = [p for p in r.json().get("pairs", []) if p.get("chainId") == "solana"]
            print(f"  New pump pairs: {len(pairs)}")
            existing = {p.get("baseToken", {}).get("address") for p in all_pairs}
            for p in pairs[:25]:
                if p.get("baseToken", {}).get("address") not in existing:
                    all_pairs.append(p)
    except Exception as e:
        print(f"  New pairs error: {e}")

    # Endpoint 4: Gainers - tangkap yang volume bangkit
    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/search?q=raydium", timeout=10)
        if r.status_code == 200:
            pairs = [p for p in r.json().get("pairs", []) if p.get("chainId") == "solana"]
            existing = {p.get("baseToken", {}).get("address") for p in all_pairs}
            for p in pairs[:20]:
                if p.get("baseToken", {}).get("address") not in existing:
                    all_pairs.append(p)
    except Exception as e:
        print(f"  Raydium error: {e}")

    print(f"  Total collected: {len(all_pairs)}")
    return all_pairs


def get_token_details(pair):
    try:
        base = pair.get("baseToken", {})
        liq = pair.get("liquidity", {})
        vol = pair.get("volume", {})
        pc = pair.get("priceChange", {})
        txns = pair.get("txns", {})

        created_at = pair.get("pairCreatedAt", 0)
        age_hours = (time.time() - created_at / 1000) / 3600 if created_at else 999

        h24 = txns.get("h24", {})
        h6 = txns.get("h6", {})
        h1 = txns.get("h1", {})

        h24_buys = h24.get("buys", 0)
        h24_sells = h24.get("sells", 0)
        h6_buys = h6.get("buys", 0)
        h6_sells = h6.get("sells", 0)
        h1_buys = h1.get("buys", 0)
        h1_sells = h1.get("sells", 0)

        # DexScreener v2: makers ada di txns per timeframe
        makers_h24 = h24.get("makers", 0) or 0
        makers_h6 = h6.get("makers", 0) or 0
        makers_h1 = h1.get("makers", 0) or 0

        # Fallback: coba dari root pair
        if makers_h24 == 0:
            makers_h24 = pair.get("makers", 0) or 0

        vol_1h = float(vol.get("h1", 0) or 0)
        vol_6h = float(vol.get("h6", 0) or 0)
        vol_24h = float(vol.get("h24", 0) or 0)

        # Wash trading: gunakan kombinasi buys vs makers
        wash_score = 0
        avg_buys_per_maker = 0
        if makers_h24 > 0 and h24_buys > 0:
            avg_buys_per_maker = h24_buys / makers_h24
            if avg_buys_per_maker > 6:
                wash_score = 3
            elif avg_buys_per_maker > 4:
                wash_score = 2
            elif avg_buys_per_maker > 2.5:
                wash_score = 1
        elif makers_h1 > 0 and h1_buys > 0:
            avg_buys_per_maker = h1_buys / makers_h1
            if avg_buys_per_maker > 6:
                wash_score = 3
            elif avg_buys_per_maker > 4:
                wash_score = 2

        # Volume acceleration: bandingkan h1 vs rata-rata per jam dari h6
        vol_accel = 0
        if vol_6h > 0:
            avg_h6_per_hour = vol_6h / 6
            if avg_h6_per_hour > 0:
                vol_accel = vol_1h / avg_h6_per_hour

        return {
            "name": base.get("name", "Unknown"),
            "symbol": base.get("symbol", "???"),
            "address": base.get("address", ""),
            "price_usd": float(pair.get("priceUsd", 0) or 0),
            "mcap": float(pair.get("marketCap", 0) or 0),
            "fdv": float(pair.get("fdv", 0) or 0),
            "liquidity_usd": float(liq.get("usd", 0) or 0),
            "volume_24h": vol_24h,
            "volume_6h": vol_6h,
            "volume_1h": vol_1h,
            "vol_accel": round(vol_accel, 1),  # volume acceleration
            "price_change_5m": float(pc.get("m5", 0) or 0),
            "price_change_1h": float(pc.get("h1", 0) or 0),
            "price_change_6h": float(pc.get("h6", 0) or 0),
            "price_change_24h": float(pc.get("h24", 0) or 0),
            "age_hours": round(age_hours, 1),
            "h24_buy_sell": round(h24_buys / max(h24_sells, 1), 2),
            "h6_buy_sell": round(h6_buys / max(h6_sells, 1), 2),
            "h1_buy_sell": round(h1_buys / max(h1_sells, 1), 2),
            "h1_buys": h1_buys,
            "h1_sells": h1_sells,
            "makers_h24": makers_h24,
            "makers_h1": makers_h1,
            "wash_score": wash_score,
            "avg_buys_per_maker": round(avg_buys_per_maker, 1),
            "pair_url": pair.get("url", ""),
            "dex_id": pair.get("dexId", ""),
        }
    except Exception as e:
        print(f"  Parse error: {e}")
        return None


# ─── FILTER ──────────────────────────────────────────────────

COPYCAT_KEYWORDS = [
    "asteroid", "pepe2", "shib2", "inu2", "elon2", "doge2",
    "classic", "real ", "fake", " copy", "v2 ", " v3",
    "2.0", "reborn", "remix", "clone", " sol" # "xxxsol" copycats
]

def passes_filter(token):
    # Hard blocks
    if token["mcap"] <= 0:
        return False, "MCap invalid"
    if token["liquidity_usd"] < config.MIN_LIQUIDITY_USD:
        return False, "Liquidity rendah"
    if token["age_hours"] > config.MAX_AGE_HOURS:
        return False, "Terlalu tua"
    if token["wash_score"] >= 3:
        return False, f"Wash trading ({token['avg_buys_per_maker']}x/wallet)"

    # Volume minimum: harus ada aktivitas SEKARANG
    if token["volume_1h"] < 1000:
        return False, "Volume 1h terlalu rendah (<$1k)"

    # Copycat check
    name_lower = token["name"].lower()
    sym_lower = token["symbol"].lower()
    for kw in COPYCAT_KEYWORDS:
        if kw in name_lower or kw in sym_lower:
            return False, f"Copycat keyword: {kw}"

    # Volume manipulasi ekstrem
    if token["volume_24h"] > 0 and token["mcap"] > 0:
        if token["volume_24h"] / token["mcap"] > 100:
            return False, "Volume/MCap >100x (manipulasi)"

    return True, "OK"


# ─── TOKEN CLASSIFIER ────────────────────────────────────────

def classify_token_type(token):
    """
    Tentukan tipe token sebelum scoring:
    - FRESH_GRADUATE: baru dari pump.fun, age < 6 jam
    - RESURRECTION: token lama yang tiba-tiba hidup lagi
    - MOMENTUM: token yang lagi naik kencang
    - ACCUMULATION: volume membangun perlahan
    """
    age = token["age_hours"]
    vol_accel = token["vol_accel"]
    is_resurrection, res_multiplier = detect_resurrection(
        token["address"], token["volume_1h"]
    )

    if age < 6 and token["volume_1h"] > 2000:
        return "FRESH_GRADUATE", res_multiplier

    if is_resurrection:
        return "RESURRECTION", res_multiplier

    if token["price_change_1h"] > 20 and token["h1_buy_sell"] > 1.5:
        return "MOMENTUM", 0

    if vol_accel > 2 and token["h1_buy_sell"] > 1.3:
        return "ACCUMULATION", 0

    return "NORMAL", 0


# ─── SCORING ENGINE ──────────────────────────────────────────

def score_token(token):
    score = 0
    reasons = []
    warnings = []

    token_type, extra_data = classify_token_type(token)

    # === FRESH GRADUATE SCORING ===
    if token_type == "FRESH_GRADUATE":
        score += 20
        reasons.append(f"🆕 Fresh graduate pump.fun ({token['age_hours']} jam)")

        # Untuk fresh graduate, yang paling penting adalah buy pressure SEKARANG
        bsr_1h = token["h1_buy_sell"]
        if bsr_1h > 3:
            score += 30
            reasons.append(f"💚 Buy pressure sangat kuat ({bsr_1h}x)")
        elif bsr_1h > 2:
            score += 20
            reasons.append(f"✅ Buy pressure kuat ({bsr_1h}x)")
        elif bsr_1h > 1.3:
            score += 10
            reasons.append(f"🟡 Buy pressure moderate ({bsr_1h}x)")

        # Volume absolut
        if token["volume_1h"] > 50000:
            score += 20
            reasons.append(f"🔥 Volume 1h sangat tinggi (${token['volume_1h']:,.0f})")
        elif token["volume_1h"] > 10000:
            score += 12
            reasons.append(f"📈 Volume 1h bagus (${token['volume_1h']:,.0f})")
        elif token["volume_1h"] > 3000:
            score += 5

        # MCap kecil = potensi besar
        if token["mcap"] < 100000:
            score += 15
            reasons.append(f"💰 MCap sangat kecil (${token['mcap']:,.0f}) - potential moonbag")
        elif token["mcap"] < 500000:
            score += 8
            reasons.append(f"💰 MCap kecil (${token['mcap']:,.0f})")

    # === RESURRECTION SCORING ===
    elif token_type == "RESURRECTION":
        score += 25
        reasons.append(f"⚡ VOLUME RESURRECTION! ({extra_data}x dari baseline)")

        bsr_1h = token["h1_buy_sell"]
        if bsr_1h > 2:
            score += 25
            reasons.append(f"💚 Buy pressure kuat post-resurrection ({bsr_1h}x)")
        elif bsr_1h > 1.3:
            score += 15
            reasons.append(f"🟡 Buy pressure ada ({bsr_1h}x)")

        if token["price_change_1h"] > 50:
            score += 20
            reasons.append(f"🚀 +{token['price_change_1h']}% dalam 1 jam!")
        elif token["price_change_1h"] > 20:
            score += 10
            reasons.append(f"📈 +{token['price_change_1h']}% dalam 1 jam")

    # === MOMENTUM SCORING ===
    elif token_type == "MOMENTUM":
        score += 10
        reasons.append(f"📈 Momentum kuat (+{token['price_change_1h']}% / 1h)")

        bsr_1h = token["h1_buy_sell"]
        if bsr_1h > 3:
            score += 25
            reasons.append(f"💚 Buy pressure sangat kuat ({bsr_1h}x)")
        elif bsr_1h > 2:
            score += 18
            reasons.append(f"✅ Buy dominan ({bsr_1h}x)")

        vol_accel = token["vol_accel"]
        if vol_accel > 4:
            score += 20
            reasons.append(f"🔥 Volume acceleration ({vol_accel}x vs rata-rata 6h)")
        elif vol_accel > 2:
            score += 12
            reasons.append(f"📊 Volume meningkat ({vol_accel}x)")

    # === ACCUMULATION / NORMAL SCORING ===
    else:
        vol_accel = token["vol_accel"]
        if vol_accel > 3:
            score += 20
            reasons.append(f"📊 Volume acceleration {vol_accel}x")
        elif vol_accel > 2:
            score += 12
            reasons.append(f"📊 Volume naik {vol_accel}x")

        bsr_1h = token["h1_buy_sell"]
        if bsr_1h > 2.5:
            score += 20
            reasons.append(f"💚 Buy dominan ({bsr_1h}x)")
        elif bsr_1h > 1.5:
            score += 12
            reasons.append(f"🟡 Sedikit buy dominan ({bsr_1h}x)")
        elif bsr_1h < 0.7:
            score -= 20
            warnings.append("🔴 Sell pressure tinggi!")

    # === UNIVERSAL CHECKS (berlaku semua tipe) ===

    # Liquidity health
    if token["mcap"] > 0:
        liq_ratio = token["liquidity_usd"] / token["mcap"]
        if liq_ratio > 0.15:
            score += 10
            reasons.append(f"💧 Liquidity sehat ({round(liq_ratio*100,1)}%)")
        elif liq_ratio > 0.05:
            score += 5
        elif liq_ratio < 0.02:
            score -= 10
            warnings.append("⚠️ Liquidity sangat tipis!")

    # Wash trading warning
    if token["wash_score"] == 2:
        score -= 15
        warnings.append(f"🔶 Wash trading suspicious ({token['avg_buys_per_maker']}x/wallet)")
    elif token["wash_score"] == 1:
        score -= 5
        warnings.append(f"⚠️ Sedikit suspicious ({token['avg_buys_per_maker']}x/wallet)")

    # Dump warning
    if token["price_change_1h"] < -25:
        score -= 20
        warnings.append(f"📉 Dump -{abs(token['price_change_1h'])}% dalam 1 jam!")

    return score, reasons, warnings, token_type


def classify_signal(score, token_type):
    """
    Signal classification berdasarkan score DAN token type
    """
    # MOONBAG: fresh graduate MCap kecil ATAU resurrection dengan score tinggi
    if score >= config.SCORE_MOONBAG:
        if token_type in ["FRESH_GRADUATE", "RESURRECTION"]:
            return "🌙 MOONBAG CANDIDATE", "MCap kecil + momentum awal = potensi 100x+"
        else:
            return "🌙 MOONBAG CANDIDATE", "Hold berminggu-minggu jika narrative kuat"

    # SWING: momentum atau accumulation yang kuat
    elif score >= config.SCORE_SWING:
        return "🎯 SWING TARGET", "Hold 1-7 hari, pantau whale"

    # SCALP: harus ada volume SEKARANG dan buy pressure
    elif score >= config.SCORE_SCALP:
        if token_type == "FRESH_GRADUATE":
            return "⚡ SCALP — FRESH GRADUATE", "Baru graduate, tangkap momentum awal"
        elif token_type == "RESURRECTION":
            return "⚡ SCALP — RESURRECTION", "Volume bangkit, tangkap wave pertama"
        else:
            return "⚡ SCALP OPPORTUNITY", "Hold <2 jam, quick profit"
    else:
        return None, None


# ─── TELEGRAM MESSAGES ───────────────────────────────────────

async def send_alert(bot, token, score, signal_type, signal_desc,
                     reasons, warnings, token_type, twitter_summary=None):

    mcap_str = "${:,.0f}".format(token["mcap"])
    liq_str = "${:,.0f}".format(token["liquidity_usd"])
    vol_1h_str = "${:,.0f}".format(token["volume_1h"])
    vol_24h_str = "${:,.0f}".format(token["volume_24h"])

    wash_labels = ["✅ Organik", "🟡 Suspicious", "🔶 High Suspicious", "🔴 Wash Trading"]
    wash_label = wash_labels[min(token["wash_score"], 3)]

    type_emoji = {
        "FRESH_GRADUATE": "🆕",
        "RESURRECTION": "⚡",
        "MOMENTUM": "🚀",
        "ACCUMULATION": "📈",
        "NORMAL": "📊"
    }.get(token_type, "📊")

    msg = f"""{signal_type}
━━━━━━━━━━━━━━━━━━━
🪙 *{token['name']}* (${token['symbol']})
{type_emoji} Type: *{token_type}*
📊 Score: *{score}/100*
💡 {signal_desc}

📈 *Market Data*
├ MCap: {mcap_str}
├ Liquidity: {liq_str}
├ Vol 1h: {vol_1h_str}
├ Vol 24h: {vol_24h_str}
├ Age: {token['age_hours']} jam
├ Buy/Sell 1h: {token['h1_buy_sell']}x
├ Vol Accel: {token['vol_accel']}x
├ Trading: {wash_label}
└ Change 1h: {token['price_change_1h']}%

✅ *Kenapa Menarik:*
{chr(10).join(reasons)}"""

    if warnings:
        msg += f"\n\n⚠️ *Warning:*\n{chr(10).join(warnings)}"

    if twitter_summary:
        msg += f"\n\n{twitter_summary}"

    msg += f"\n\n🔗 [Chart & Trade]({token['pair_url']})"
    msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ─── SCAN LOGIC ──────────────────────────────────────────────

async def do_scan(bot, manual=False):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{timestamp}] {'Manual' if manual else 'Auto'} scan...")

    if manual:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=f"🔄 *Manual Scan Dimulai*\n⏳ Scanning... ({timestamp} WIB)",
            parse_mode=ParseMode.MARKDOWN
        )

    pairs = get_solana_pairs()
    alerts_sent = 0
    filtered_count = 0

    for pair in pairs:
        token = get_token_details(pair)
        if not token or not token["address"]:
            continue

        # Update volume history untuk resurrection detection
        track_volume(token["address"], token["volume_1h"])

        # Deduplicate
        token_key = token["address"] + str(round(token["mcap"] / 5000))
        if token_key in seen_tokens:
            continue

        # Filter
        passed, reason = passes_filter(token)
        if not passed:
            print(f"  ✗ {token['name']} — {reason}")
            filtered_count += 1
            continue

        # Score
        score, reasons, warnings, token_type = score_token(token)
        signal_type, signal_desc = classify_signal(score, token_type)

        print(f"  {token_type[:4]} | {token['name'][:15]} | Score:{score} | MCap:${token['mcap']:,.0f} | V1h:${token['volume_1h']:,.0f}")

        if signal_type:
            seen_tokens.add(token_key)

            # Twitter check (hanya untuk signal yang cukup kuat)
            twitter_bonus = 0
            twitter_summary = None
            if score >= 55 and config.ENABLE_TWITTER_CHECK:
                print(f"    → Checking Twitter for {token['symbol']}...")
                twitter_bonus, twitter_summary = analyze_twitter_signal(
                    token["name"], token["symbol"], token["address"]
                )
                score += twitter_bonus

            print(f"  >>> ALERT: {signal_type} (final score: {score})")
            await send_alert(bot, token, score, signal_type, signal_desc,
                           reasons, warnings, token_type, twitter_summary)
            alerts_sent += 1
            await asyncio.sleep(1.5)

    if manual:
        msg = f"""✅ *Scan Selesai*
━━━━━━━━━━━━━━━━━━━
📊 Dipindai: {len(pairs)} token
🚫 Difilter: {filtered_count} token
🔔 Alert: {alerts_sent} sinyal
⏰ {datetime.now().strftime('%H:%M:%S')} WIB"""
        if alerts_sent == 0:
            msg += "\n\n💤 Belum ada koin yang memenuhi kriteria saat ini.\n_Coba lagi dalam beberapa menit._"
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode=ParseMode.MARKDOWN
        )

    # Bersihkan cache kalau terlalu besar
    if len(seen_tokens) > 1000:
        seen_tokens.clear()
        print("  Cache cleared")

    print(f"  Done: {alerts_sent} alerts, {filtered_count} filtered")
    return alerts_sent


# ─── COMMAND HANDLERS ────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v3*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "*Commands:*\n"
        "/scan — Scan manual sekarang\n"
        "/status — Cek status bot\n"
        "/filter — Lihat filter aktif\n"
        "/help — Bantuan",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
        return
    await do_scan(context.bot, manual=True)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
        return
    twitter_status = "✅ ON" if config.ENABLE_TWITTER_CHECK else "❌ OFF"
    await update.message.reply_text(
        f"✅ *Bot Status: ONLINE*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Auto scan: setiap {config.CHECK_INTERVAL_MINUTES} menit\n"
        f"📝 Cache: {len(seen_tokens)} token\n"
        f"📱 Twitter check: {twitter_status}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"*Thresholds:*\n"
        f"🌙 Moonbag ≥{config.SCORE_MOONBAG} | 🎯 Swing ≥{config.SCORE_SWING} | ⚡ Scalp ≥{config.SCORE_SCALP}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
        return
    await update.message.reply_text(
        f"🔍 *Filter Aktif*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Min Liquidity: ${config.MIN_LIQUIDITY_USD:,}\n"
        f"📊 Min Volume 1h: $1,000\n"
        f"⏰ Max Age: {config.MAX_AGE_HOURS} jam\n"
        f"🛡️ Anti wash trading: ON\n"
        f"🚫 Anti copycat: ON\n\n"
        f"*Token Types Dideteksi:*\n"
        f"🆕 Fresh Graduate (<6 jam)\n"
        f"⚡ Volume Resurrection\n"
        f"🚀 Momentum\n"
        f"📈 Accumulation",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ─── BACKGROUND SCANNER ──────────────────────────────────────

async def background_scanner(bot):
    interval = config.CHECK_INTERVAL_MINUTES * 60
    while True:
        await asyncio.sleep(interval)
        print(f"\n[AUTO SCAN] {datetime.now().strftime('%H:%M:%S')}")
        await do_scan(bot, manual=False)


# ─── MAIN ────────────────────────────────────────────────────

async def main():
    print("=" * 50)
    print("  MEMECOIN MONITOR v3 - Solana")
    print("=" * 50)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("help", cmd_help))

    async with app:
        await app.start()
        bot = app.bot

        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text="🤖 *MEMECOIN MONITOR v3 AKTIF*\n"
                 "━━━━━━━━━━━━━━━━━━━\n"
                 "✅ Fresh Graduate detection: ON\n"
                 "✅ Volume Resurrection detection: ON\n"
                 "✅ Anti wash trading: ON\n"
                 "✅ Twitter/X mention check: ON\n\n"
                 "/scan — scan manual\n"
                 "⏳ Auto scan pertama dimulai...",
            parse_mode=ParseMode.MARKDOWN
        )

        await do_scan(bot, manual=False)

        await app.updater.start_polling(drop_pending_updates=True)
        await background_scanner(bot)


if __name__ == "__main__":
    asyncio.run(main())
