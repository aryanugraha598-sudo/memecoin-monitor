import requests
import time
import asyncio
import re
import json
import os
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import config

# ══════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════
volume_history  = {}   # address -> [(timestamp, vol_1h)]
seen_addresses  = {}   # address -> timestamp (TTL-based dedup)
hold_watchlist  = set()  # token address yang sedang di-hold user
sm_wallets      = []   # smart money wallet list (runtime, sync dari config + /wallet add)

SEEN_TTL_HOURS  = 6
STATE_FILE      = "state.json"

# Track kapan terakhir SM wallet tertentu beli sesuatu
sm_last_buy     = {}   # wallet_address -> {"token": addr, "name": str, "time": ts}


# ══════════════════════════════════════════════════════════════
#  PERSISTENT STATE
# ══════════════════════════════════════════════════════════════

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "seen_addresses": seen_addresses,
                "volume_history": {k: [[t, v] for t, v in vals]
                                   for k, vals in volume_history.items()},
                "hold_watchlist": list(hold_watchlist),
                "sm_wallets":     sm_wallets,
                "sm_last_buy":    sm_last_buy,
            }, f)
    except Exception as e:
        print(f"  Save state error: {e}")


def load_state():
    global seen_addresses, volume_history, hold_watchlist, sm_wallets, sm_last_buy
    # Mulai dari config SMART_MONEY_WALLETS sebagai base
    sm_wallets.clear()
    sm_wallets.extend(config.SMART_MONEY_WALLETS)

    if not os.path.exists(STATE_FILE):
        print("  No state file, starting fresh.")
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        seen_addresses = data.get("seen_addresses", {})
        volume_history = {k: [(t, v) for t, v in vals]
                          for k, vals in data.get("volume_history", {}).items()}
        hold_watchlist = set(data.get("hold_watchlist", []))
        sm_last_buy    = data.get("sm_last_buy", {})

        # Merge: gabungkan wallet dari state + config (no duplikat)
        saved_wallets = data.get("sm_wallets", [])
        for w in saved_wallets:
            if w not in sm_wallets:
                sm_wallets.append(w)

        print(f"  Loaded: {len(seen_addresses)} seen | {len(hold_watchlist)} hold | {len(sm_wallets)} SM wallets")
    except Exception as e:
        print(f"  Load state error: {e}")


# ══════════════════════════════════════════════════════════════
#  TTL DEDUP
# ══════════════════════════════════════════════════════════════

def is_seen(address):
    if address not in seen_addresses:
        return False
    if (time.time() - seen_addresses[address]) / 3600 > SEEN_TTL_HOURS:
        del seen_addresses[address]
        return False
    return True

def mark_seen(address):
    seen_addresses[address] = time.time()

def cleanup_seen():
    now = time.time()
    expired = [k for k, t in seen_addresses.items()
               if (now - t) / 3600 > SEEN_TTL_HOURS]
    for k in expired:
        del seen_addresses[k]
    if expired:
        print(f"  Cleaned {len(expired)} expired seen entries")


# ══════════════════════════════════════════════════════════════
#  VOLUME TRACKING
# ══════════════════════════════════════════════════════════════

def track_volume(address, volume_1h):
    now = time.time()
    if address not in volume_history:
        volume_history[address] = []
    volume_history[address].append((now, volume_1h))
    volume_history[address] = volume_history[address][-12:]

def detect_resurrection(address, current_vol_1h):
    if address not in volume_history or len(volume_history[address]) < 3:
        return False, 0
    history = volume_history[address]
    prev    = [v for _, v in history[:-1]]
    avg     = sum(prev) / len(prev)
    if avg < 200 and current_vol_1h > 5000:
        return True, round(current_vol_1h / max(avg, 1))
    return False, 0


# ══════════════════════════════════════════════════════════════
#  HELIUS API — SMART MONEY INTELLIGENCE
# ══════════════════════════════════════════════════════════════

def helius_get(endpoint, params=None):
    """Generic Helius API call. Return JSON atau None jika gagal."""
    if not config.HELIUS_API_KEY:
        return None
    base = "https://api.helius.xyz/v0"
    try:
        p = params or {}
        p["api-key"] = config.HELIUS_API_KEY
        r = requests.get(f"{base}/{endpoint}", params=p, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  Helius err: {e}")
    return None


def get_early_buyers(token_address, pair_address=None, limit=50):
    """
    Ambil daftar wallet yang beli token ini di awal.
    Pakai Helius transaction history dari pair address.
    Return: set of wallet addresses
    """
    if not config.HELIUS_API_KEY or not config.ENABLE_SMART_MONEY_CHECK:
        return set()

    buyers = set()
    try:
        # Coba dari token address langsung
        data = helius_get(f"addresses/{token_address}/transactions",
                          {"type": "SWAP", "limit": limit})
        if not data:
            return set()

        for tx in data:
            # Format Helius enhanced transaction
            account_data = tx.get("accountData", [])
            for acc in account_data:
                wallet = acc.get("account", "")
                if wallet and len(wallet) > 30:
                    buyers.add(wallet)

            # Juga cek dari fee payer (biasanya initiator transaksi)
            fee_payer = tx.get("feePayer", "")
            if fee_payer:
                buyers.add(fee_payer)

    except Exception as e:
        print(f"  get_early_buyers err: {e}")

    return buyers


def check_smart_money_in_token(token_address, token_name=""):
    """
    Cek apakah ada SM wallet yang beli token ini.
    Return: (count, list_of_sm_wallets_found)
    """
    if not sm_wallets or not config.ENABLE_SMART_MONEY_CHECK:
        return 0, []
    if not config.HELIUS_API_KEY:
        return 0, []

    early_buyers = get_early_buyers(token_address)
    if not early_buyers:
        return 0, []

    found = [w for w in sm_wallets if w in early_buyers]
    if found:
        print(f"  🧠 SM found in {token_name}: {len(found)} wallet(s)")
    return len(found), found


def get_wallet_recent_buys(wallet_address, limit=5):
    """
    Cek transaksi swap terbaru dari sebuah wallet.
    Return: list of token addresses yang baru di-beli
    """
    if not config.HELIUS_API_KEY:
        return []

    data = helius_get(f"addresses/{wallet_address}/transactions",
                      {"type": "SWAP", "limit": limit})
    if not data:
        return []

    recent_tokens = []
    for tx in data:
        ts = tx.get("timestamp", 0)
        # Hanya cek transaksi dalam 30 menit terakhir
        if time.time() - ts > 1800:
            continue

        events = tx.get("events", {})
        swap   = events.get("swap", {})

        # Token yang di-beli (output token)
        token_out = swap.get("tokenOutputs", [])
        for t in token_out:
            mint = t.get("mint", "")
            if mint and mint != "So11111111111111111111111111111111111111112":
                # Bukan SOL native — ini token yang dibeli
                recent_tokens.append({
                    "address":   mint,
                    "wallet":    wallet_address,
                    "timestamp": ts,
                    "amount_usd": t.get("tokenAmount", 0),
                })

    return recent_tokens


# ══════════════════════════════════════════════════════════════
#  NITTER SCRAPER (Twitter)
# ══════════════════════════════════════════════════════════════

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.lucahammer.com",
]

def search_twitter_mentions(symbol, address):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    tweets  = []
    for query in [f"${symbol}", address[:20]]:
        for instance in NITTER_INSTANCES:
            try:
                url = f"{instance}/search?q={requests.utils.quote(query)}&f=tweets"
                r   = requests.get(url, headers=headers, timeout=6)
                if r.status_code != 200:
                    continue
                matches = re.findall(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>', r.text, re.DOTALL)
                for m in matches[:4]:
                    clean = re.sub(r'<[^>]+>', '', m).strip()
                    clean = re.sub(r'\s+', ' ', clean)
                    if len(clean) > 10:
                        tweets.append(clean)
                if tweets:
                    break
            except:
                continue
        if tweets:
            break
    return tweets

def analyze_twitter(name, symbol, address):
    tweets = search_twitter_mentions(symbol, address)
    if not tweets:
        return 0, "❌ Tidak ada mention Twitter"

    pos_words = ["moon","gem","100x","buy","bullish","fire","🔥","🚀","💎","ape","alpha","early","send","go","launch"]
    neg_words = ["rug","scam","dump","rekt","dead","honeypot","avoid","warning","careful","sus","fake"]

    pos  = sum(1 for t in tweets for w in pos_words if w in t.lower())
    neg  = sum(1 for t in tweets for w in neg_words if w in t.lower())
    bonus = min(len(tweets) * 2, 10)
    sentiment = "🟢 Positif" if pos > neg else "🔴 Negatif" if neg > pos else "🟡 Netral"
    if neg > pos:
        bonus -= 10

    return bonus, f"📱 Twitter: {len(tweets)} mention | {sentiment}"


# ══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════

def get_solana_pairs():
    all_pairs = []
    seen_addr = set()

    def add_pair(pair):
        addr = pair.get("baseToken", {}).get("address", "")
        if addr and addr not in seen_addr:
            all_pairs.append(pair)
            seen_addr.add(addr)

    def fetch_token_best_pair(addr):
        try:
            r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
            if r.status_code == 200:
                pairs     = r.json().get("pairs", [])
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if sol_pairs:
                    return max(sol_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
        except:
            pass
        return None

    # EP1: Token profiles (pump.fun graduates)
    try:
        r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        if r.status_code == 200:
            tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Profiles: {len(tokens)}")
            for t in tokens[:25]:
                p = fetch_token_best_pair(t.get("tokenAddress", ""))
                if p:
                    add_pair(p)
                time.sleep(0.15)
    except Exception as e:
        print(f"  EP1 err: {e}")

    # EP2: Boosted tokens
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        if r.status_code == 200:
            tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Boosted: {len(tokens)}")
            for t in tokens[:20]:
                p = fetch_token_best_pair(t.get("tokenAddress", ""))
                if p:
                    add_pair(p)
                time.sleep(0.15)
    except Exception as e:
        print(f"  EP2 err: {e}")

    # EP3: Fresh pump.fun graduates
    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/search?q=pump+sol", timeout=10)
        if r.status_code == 200:
            for p in r.json().get("pairs", []):
                if p.get("chainId") == "solana":
                    add_pair(p)
    except Exception as e:
        print(f"  EP3 err: {e}")

    # EP4: Raydium pairs
    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/search?q=raydium", timeout=10)
        if r.status_code == 200:
            for p in r.json().get("pairs", []):
                if p.get("chainId") == "solana":
                    add_pair(p)
    except Exception as e:
        print(f"  EP4 err: {e}")

    print(f"  Total pairs: {len(all_pairs)}")
    return all_pairs


def get_token_details(pair):
    try:
        base = pair.get("baseToken", {})
        liq  = pair.get("liquidity", {})
        vol  = pair.get("volume", {})
        pc   = pair.get("priceChange", {})
        txns = pair.get("txns", {})

        created_at = pair.get("pairCreatedAt", 0)
        age_h = (time.time() - created_at / 1000) / 3600 if created_at else 999

        h1  = txns.get("h1", {})
        h6  = txns.get("h6", {})
        h24 = txns.get("h24", {})

        h1_buys  = h1.get("buys", 0);   h1_sells  = h1.get("sells", 0)
        h6_buys  = h6.get("buys", 0);   h6_sells  = h6.get("sells", 0)
        h24_buys = h24.get("buys", 0);  h24_sells = h24.get("sells", 0)

        makers = (h24.get("makers") or h6.get("makers") or
                  h1.get("makers") or pair.get("makers") or 0)

        v1h  = float(vol.get("h1", 0) or 0)
        v6h  = float(vol.get("h6", 0) or 0)
        v24h = float(vol.get("h24", 0) or 0)

        avg_6h_per_h = v6h / 6 if v6h > 0 else 0
        vol_accel    = round(v1h / avg_6h_per_h, 2) if avg_6h_per_h > 50 else 0

        wash = 0; abpm = 0
        if makers > 0 and h24_buys > 0:
            abpm = h24_buys / makers
            if abpm > 6:     wash = 3
            elif abpm > 4:   wash = 2
            elif abpm > 2.5: wash = 1

        return {
            "name":          base.get("name", "Unknown"),
            "symbol":        base.get("symbol", "???"),
            "address":       base.get("address", ""),
            "pair_address":  pair.get("pairAddress", ""),
            "mcap":          float(pair.get("marketCap", 0) or 0),
            "liquidity_usd": float(liq.get("usd", 0) or 0),
            "v1h": v1h, "v6h": v6h, "v24h": v24h,
            "vol_accel":     vol_accel,
            "pc_1h":  float(pc.get("h1", 0) or 0),
            "pc_6h":  float(pc.get("h6", 0) or 0),
            "pc_24h": float(pc.get("h24", 0) or 0),
            "pc_5m":  float(pc.get("m5", 0) or 0),
            "age_h":  round(age_h, 1),
            "h1_bsr":  round(h1_buys  / max(h1_sells,  1), 2),
            "h6_bsr":  round(h6_buys  / max(h6_sells,  1), 2),
            "h24_bsr": round(h24_buys / max(h24_sells, 1), 2),
            "makers":  makers,
            "wash":    wash,
            "abpm":    round(abpm, 1),
            "pair_url": pair.get("url", ""),
        }
    except Exception as e:
        print(f"  Parse err: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  HARD FILTER
# ══════════════════════════════════════════════════════════════

BLOCK_NAMES = [
    "asteroid","pepe2","shib2","inu2","classic","fake","copy",
    "v2 "," v3","2.0","reborn","remix","clone","generational","rekt",
]

def passes_filter(token):
    if token["mcap"] <= 0:                          return False, "MCap invalid"
    if token["liquidity_usd"] < config.MIN_LIQUIDITY_USD: return False, "Liquidity rendah"
    if token["age_h"] > config.MAX_AGE_HOURS:       return False, "Terlalu tua"
    if token["wash"] >= 3:                          return False, f"Wash trading ({token['abpm']}x/wallet)"
    if token["v1h"] < 1000:                         return False, "Vol 1h < $1k"
    if token["pc_1h"] < -5 and token["vol_accel"] < 1.0:
        return False, f"Distribusi: -{abs(token['pc_1h'])}% + vol accel {token['vol_accel']}x"
    if token["pc_1h"] < -10 and token["pc_6h"] < -15:
        return False, f"Downtrend: 1h={token['pc_1h']}% 6h={token['pc_6h']}%"
    name_l = (token["name"] + " " + token["symbol"]).lower()
    for kw in BLOCK_NAMES:
        if kw in name_l:
            return False, f"Block keyword: {kw}"
    if token["v24h"] > 0 and token["mcap"] > 0:
        if token["v24h"] / token["mcap"] > 100:
            return False, "Vol/MCap >100x"
    return True, "OK"


# ══════════════════════════════════════════════════════════════
#  SCORING
# ══════════════════════════════════════════════════════════════

def score_token(token):
    score    = 0
    reasons  = []
    warnings = []

    is_res, res_mult = detect_resurrection(token["address"], token["v1h"])

    age = token["age_h"]
    if age < 6 and token["v1h"] > 2000:
        ttype = "FRESH_GRADUATE"
    elif is_res:
        ttype = "RESURRECTION"
    elif token["pc_1h"] > 15 and token["h1_bsr"] > 1.5:
        ttype = "MOMENTUM"
    elif token["vol_accel"] > 2 and token["h1_bsr"] > 1.3:
        ttype = "ACCUMULATION"
    else:
        ttype = "NORMAL"

    if ttype == "FRESH_GRADUATE":
        score += 20; reasons.append(f"🆕 Fresh graduate ({age} jam)")
        if token["h1_bsr"] > 3:    score += 30; reasons.append(f"💚 Buy pressure kuat ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 2:  score += 20; reasons.append(f"✅ Buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.3:score += 10; reasons.append(f"🟡 Buy moderate ({token['h1_bsr']}x)")
        if token["v1h"] > 50000:   score += 20; reasons.append(f"🔥 Vol 1h: ${token['v1h']:,.0f}")
        elif token["v1h"] > 10000: score += 12; reasons.append(f"📈 Vol 1h: ${token['v1h']:,.0f}")
        elif token["v1h"] > 3000:  score += 5
        if token["mcap"] < 100000: score += 15; reasons.append(f"💰 MCap kecil (${token['mcap']:,.0f})")
        elif token["mcap"] < 500000: score += 8; reasons.append(f"💰 MCap (${token['mcap']:,.0f})")

    elif ttype == "RESURRECTION":
        score += 30; reasons.append(f"⚡ RESURRECTION! Volume {res_mult}x dari baseline")
        if token["h1_bsr"] > 2:    score += 25; reasons.append(f"💚 Buy pressure post-res ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.3:score += 15; reasons.append(f"🟡 Buy ada ({token['h1_bsr']}x)")
        if token["pc_1h"] > 50:    score += 20; reasons.append(f"🚀 +{token['pc_1h']}% dalam 1 jam!")
        elif token["pc_1h"] > 20:  score += 10; reasons.append(f"📈 +{token['pc_1h']}%")

    elif ttype == "MOMENTUM":
        score += 10; reasons.append(f"🚀 Momentum +{token['pc_1h']}%")
        if token["h1_bsr"] > 3:    score += 25; reasons.append(f"💚 Buy pressure ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 2:  score += 18; reasons.append(f"✅ Buy dominan ({token['h1_bsr']}x)")
        if token["vol_accel"] > 4: score += 20; reasons.append(f"🔥 Vol accel {token['vol_accel']}x")
        elif token["vol_accel"] > 2:score += 12; reasons.append(f"📊 Vol accel {token['vol_accel']}x")

    else:  # ACCUMULATION / NORMAL
        if token["vol_accel"] > 3:  score += 20; reasons.append(f"📊 Vol accel {token['vol_accel']}x")
        elif token["vol_accel"] > 2:score += 12; reasons.append(f"📊 Vol accel {token['vol_accel']}x")
        if token["h1_bsr"] > 2.5:   score += 20; reasons.append(f"💚 Buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.5: score += 12; reasons.append(f"🟡 Sedikit buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] < 0.7: score -= 20; warnings.append("🔴 Sell pressure tinggi!")

    # Universal
    if token["mcap"] > 0:
        lr = token["liquidity_usd"] / token["mcap"]
        if lr > 0.15:    score += 10; reasons.append(f"💧 Liquidity sehat ({round(lr*100,1)}%)")
        elif lr > 0.05:  score += 5
        elif lr < 0.02:  score -= 10; warnings.append("⚠️ Liquidity tipis!")

    if token["wash"] == 2:   score -= 15; warnings.append(f"🔶 Wash trading ({token['abpm']}x/wallet)")
    elif token["wash"] == 1: score -= 5;  warnings.append(f"⚠️ Suspicious ({token['abpm']}x/wallet)")

    if token["pc_1h"] < -25: score -= 20; warnings.append(f"📉 Dump -{abs(token['pc_1h'])}% 1h!")
    if token["pc_5m"] > 5:   score += 5;  reasons.append(f"🟢 +{token['pc_5m']}% dalam 5 menit")

    return score, reasons, warnings, ttype


def get_signal(score, ttype):
    if score >= config.SCORE_MOONBAG:
        desc = "MCap kecil + momentum = potensi 100x+" if ttype in ["FRESH_GRADUATE","RESURRECTION"] else "Hold berminggu-minggu"
        return "🌙 MOONBAG CANDIDATE", desc
    elif score >= config.SCORE_SWING:
        return "🎯 SWING TARGET", "Hold 1-7 hari"
    elif score >= config.SCORE_SCALP:
        labels = {
            "FRESH_GRADUATE": ("⚡ SCALP — FRESH GRADUATE", "Baru graduate, tangkap momentum"),
            "RESURRECTION":   ("⚡ SCALP — RESURRECTION",   "Volume bangkit, wave pertama"),
            "MOMENTUM":       ("⚡ SCALP — MOMENTUM",        "Momentum kuat, exit cepat"),
        }
        return labels.get(ttype, ("⚡ SCALP", "Hold <2 jam"))
    return None, None


# ══════════════════════════════════════════════════════════════
#  TELEGRAM ALERTS
# ══════════════════════════════════════════════════════════════

async def send_alert(bot, token, score, sig, desc, reasons, warnings, ttype,
                     tw=None, sm_count=0, sm_wallets_found=None):
    wash_l  = ["✅ Organik","🟡 Suspicious","🔶 High Suspicious","🔴 Wash"][min(token["wash"],3)]
    ttype_e = {"FRESH_GRADUATE":"🆕","RESURRECTION":"⚡","MOMENTUM":"🚀","ACCUMULATION":"📈"}.get(ttype,"📊")

    sm_line = ""
    if sm_count > 0:
        sm_line = f"\n🧠 Smart Money: *{sm_count} wallet* detected ({'⚠️ 1 SM' if sm_count==1 else '🔥 MULTIPLE SM'})\n"

    msg = (
        f"{sig}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token['name']}* (${token['symbol']})\n"
        f"{ttype_e} Type: *{ttype}*\n"
        f"📊 Score: *{score}/100*\n"
        f"💡 {desc}\n"
        f"{sm_line}\n"
        f"📈 *Market Data*\n"
        f"├ MCap: ${token['mcap']:,.0f}\n"
        f"├ Liquidity: ${token['liquidity_usd']:,.0f}\n"
        f"├ Vol 1h: ${token['v1h']:,.0f}\n"
        f"├ Vol 24h: ${token['v24h']:,.0f}\n"
        f"├ Age: {token['age_h']} jam\n"
        f"├ Buy/Sell 1h: {token['h1_bsr']}x\n"
        f"├ Vol Accel: {token['vol_accel']}x\n"
        f"├ Trading: {wash_l}\n"
        f"├ 5m: {token['pc_5m']}% | 1h: {token['pc_1h']}% | 6h: {token['pc_6h']}%\n"
        f"└ MCap: ${token['mcap']:,.0f}\n\n"
        f"✅ *Kenapa Menarik:*\n" + "\n".join(reasons)
    )
    if warnings:
        msg += "\n\n⚠️ *Warning:*\n" + "\n".join(warnings)
    if tw:
        msg += f"\n\n{tw}"
    msg += f"\n\n🔗 [Chart & Trade]({token['pair_url']})"
    msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ══════════════════════════════════════════════════════════════
#  HOLD WATCHLIST MONITORING
# ══════════════════════════════════════════════════════════════

def fetch_pair_by_address(address):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=8)
        if r.status_code != 200:
            return None
        pairs     = r.json().get("pairs", [])
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None
        return max(sol_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
    except:
        return None


async def check_hold_watchlist(bot):
    if not hold_watchlist:
        return
    print(f"  Checking {len(hold_watchlist)} hold positions...")
    for address in list(hold_watchlist):
        try:
            pair = fetch_pair_by_address(address)
            if not pair:
                continue
            t = get_token_details(pair)
            if not t:
                continue

            track_volume(t["address"], t["v1h"])
            score, reasons, warnings, ttype = score_token(t)

            if score >= 60:   status_emoji, status_text = "🟢", "Kondisi BAGUS — hold lanjut"
            elif score >= 40: status_emoji, status_text = "🟡", "Kondisi MODERAT — pantau ketat"
            else:             status_emoji, status_text = "🔴", "Kondisi MELEMAH — pertimbangkan EXIT!"

            hold_msg = (
                f"📋 *HOLD MONITOR UPDATE*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{t['name']}* (${t['symbol']})\n"
                f"{status_emoji} Score: *{score}/100* — {status_text}\n\n"
                f"📈 *Market Data*\n"
                f"├ 5m: {t['pc_5m']}% | 1h: {t['pc_1h']}% | 6h: {t['pc_6h']}%\n"
                f"├ Vol 1h: ${t['v1h']:,.0f}\n"
                f"├ Vol Accel: {t['vol_accel']}x\n"
                f"├ Buy/Sell 1h: {t['h1_bsr']}x\n"
                f"├ Liquidity: ${t['liquidity_usd']:,.0f}\n"
                f"└ MCap: ${t['mcap']:,.0f}\n"
            )
            if score < 30:
                hold_msg += "\n⛔ *WARNING: Kondisi sangat lemah — pertimbangkan EXIT sekarang!*"
            elif warnings:
                hold_msg += "\n\n⚠️ *Warning:*\n" + "\n".join(warnings)

            hold_msg += f"\n\n🔗 [Chart & Trade]({t['pair_url']})"
            hold_msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
            hold_msg += f"\n\n_Hapus: /hold remove {address}_"

            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=hold_msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  Hold check err {address[:8]}: {e}")


# ══════════════════════════════════════════════════════════════
#  SMART MONEY WALLET ACTIVITY POLLER
# ══════════════════════════════════════════════════════════════

async def poll_smart_money_wallets(bot):
    """
    Background task: cek aktivitas terbaru setiap SM wallet.
    Kalau ada SM wallet yang beli token baru dalam 30 menit → kirim alert.
    """
    if not sm_wallets or not config.HELIUS_API_KEY:
        return

    print(f"  Polling {len(sm_wallets)} SM wallets...")
    for wallet in sm_wallets:
        try:
            recent = get_wallet_recent_buys(wallet, limit=5)
            for buy in recent:
                token_addr = buy["address"]
                ts         = buy["timestamp"]

                # Cek apakah ini pembelian baru (belum pernah kita alert dari wallet ini)
                prev = sm_last_buy.get(wallet, {})
                if prev.get("token") == token_addr:
                    continue  # sudah pernah alert

                # Update last buy
                sm_last_buy[wallet] = {"token": token_addr, "time": ts}

                # Ambil info token
                pair = fetch_pair_by_address(token_addr)
                if not pair:
                    continue
                t = get_token_details(pair)
                if not t:
                    continue

                score, reasons, warnings, ttype = score_token(t)
                wallet_short = wallet[:6] + "..." + wallet[-4:]
                ts_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')

                alert_msg = (
                    f"🧠 *SMART MONEY ALERT*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"💼 Wallet: `{wallet_short}` baru saja beli!\n"
                    f"⏰ {ts_str} WIB\n\n"
                    f"🪙 *{t['name']}* (${t['symbol']})\n"
                    f"📊 Score bot: *{score}/100*\n\n"
                    f"📈 *Market Data*\n"
                    f"├ MCap: ${t['mcap']:,.0f}\n"
                    f"├ Liquidity: ${t['liquidity_usd']:,.0f}\n"
                    f"├ Vol 1h: ${t['v1h']:,.0f}\n"
                    f"├ Age: {t['age_h']} jam\n"
                    f"├ Buy/Sell 1h: {t['h1_bsr']}x\n"
                    f"└ Vol Accel: {t['vol_accel']}x\n\n"
                )
                if warnings:
                    alert_msg += "⚠️ *Warning:*\n" + "\n".join(warnings) + "\n\n"
                alert_msg += f"🔗 [Chart & Trade]({t['pair_url']})"
                alert_msg += f"\n\n_/hold {token_addr} — pantau koin ini_"

                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=alert_msg,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                await asyncio.sleep(1.5)

        except Exception as e:
            print(f"  SM poll err {wallet[:8]}: {e}")

    save_state()


# ══════════════════════════════════════════════════════════════
#  MAIN SCAN
# ══════════════════════════════════════════════════════════════

async def do_scan(bot, manual=False):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{ts}] {'Manual' if manual else 'Auto'} scan")

    if manual:
        sm_status = f"🧠 SM wallets: {len(sm_wallets)}" if sm_wallets else "🧠 SM wallets: 0 (belum ada)"
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(f"🔄 *Manual Scan Dimulai* ({ts} WIB)\n"
                  f"⏳ Sedang memindai...\n{sm_status}"),
            parse_mode=ParseMode.MARKDOWN
        )

    pairs = get_solana_pairs()
    sent            = 0
    filtered        = 0
    low_score       = 0   # ✅ FIX: counter koin lolos filter tapi skor rendah
    already_seen    = 0

    for pair in pairs:
        t = get_token_details(pair)
        if not t or not t["address"]:
            continue

        track_volume(t["address"], t["v1h"])

        if is_seen(t["address"]):
            already_seen += 1
            continue

        ok, reason = passes_filter(t)
        if not ok:
            print(f"  ✗ {t['name'][:18]:<18} {reason}")
            filtered += 1
            continue

        score, reasons, warnings, ttype = score_token(t)
        sig, desc = get_signal(score, ttype)

        print(f"  {ttype[:4]} | {t['name'][:15]:<15} | S:{score:>3} | MC:${t['mcap']:>10,.0f} | V1h:${t['v1h']:>8,.0f} | BSR:{t['h1_bsr']}")

        if not sig:
            low_score += 1   # ✅ FIX: dihitung sekarang
            print(f"         └─ Skor rendah ({score}), tidak di-alert")
            continue

        mark_seen(t["address"])

        # ── Smart Money Check ─────────────────────────────────
        sm_count, sm_found = 0, []
        if config.ENABLE_SMART_MONEY_CHECK and sm_wallets and config.HELIUS_API_KEY:
            sm_count, sm_found = check_smart_money_in_token(t["address"], t["name"])
            if sm_count > 0:
                bonus = min(sm_count * config.SMART_MONEY_SCORE_BONUS, config.SMART_MONEY_MAX_BONUS)
                score += bonus
                reasons.append(f"🧠 Smart Money: {sm_count} wallet detected (+{bonus} pts)")
                # Recalculate signal setelah bonus
                sig, desc = get_signal(score, ttype)

        # ── Twitter Check ─────────────────────────────────────
        tw_bonus, tw_summary = 0, None
        if score >= 55 and config.ENABLE_TWITTER_CHECK:
            tw_bonus, tw_summary = analyze_twitter(t["name"], t["symbol"], t["address"])
            score += tw_bonus

        print(f"  >>> {sig} (score={score})")
        await send_alert(bot, t, score, sig, desc, reasons, warnings, ttype,
                         tw_summary, sm_count, sm_found)
        sent += 1
        await asyncio.sleep(1.5)

    # Hold watchlist check
    await check_hold_watchlist(bot)

    if manual:
        hold_note = f"\n📋 Hold watchlist: {len(hold_watchlist)} koin" if hold_watchlist else ""
        sm_note   = f"\n🧠 SM wallets aktif: {len(sm_wallets)}" if sm_wallets else ""
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"✅ *Scan Selesai*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Dipindai: {len(pairs)}\n"
                f"🔁 Sudah seen (TTL): {already_seen}\n"   # ✅ FIX: tampil sekarang
                f"🚫 Difilter: {filtered}\n"
                f"📉 Skor rendah: {low_score}\n"            # ✅ FIX: tampil sekarang
                f"🔔 Alert: {sent}\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
                f"{hold_note}{sm_note}"
                + ("\n\n💤 Belum ada koin memenuhi kriteria." if sent == 0 else "")
            ),
            parse_mode=ParseMode.MARKDOWN
        )

    # Cleanup
    cleanup_seen()
    if len(volume_history) > 2000:
        oldest = sorted(volume_history, key=lambda k: volume_history[k][-1][0])[:500]
        for k in oldest:
            del volume_history[k]

    save_state()
    print(f"  Done: {sent} alerts | {filtered} filtered | {low_score} low score | {already_seen} seen")


# ══════════════════════════════════════════════════════════════
#  TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v6*\n━━━━━━━━━━━━━━━━━━━\n"
        "*/scan* — Scan manual\n"
        "*/hold <address>* — Pantau koin yang di-hold\n"
        "*/hold list* — Lihat hold watchlist\n"
        "*/hold remove <address>* — Hapus dari hold\n"
        "*/hold check* — Cek kondisi hold sekarang\n"
        "*/wallet add <address>* — Tambah SM wallet\n"
        "*/wallet remove <address>* — Hapus SM wallet\n"
        "*/wallet list* — Lihat SM wallet list\n"
        "*/wallet check* — Poll SM wallets sekarang\n"
        "*/status* — Status bot\n"
        "*/filter* — Filter aktif\n"
        "*/clearcache* — Reset seen cache\n",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await do_scan(context.bot, manual=True)


async def cmd_hold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args
    if not args:
        await update.message.reply_text(
            f"📋 *Hold Watchlist* ({len(hold_watchlist)} koin)\n"
            "/hold <address> — tambah\n/hold list — lihat\n"
            "/hold remove <address> — hapus\n/hold check — cek sekarang",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if args[0] == "list":
        if not hold_watchlist:
            await update.message.reply_text("📋 Watchlist kosong.")
        else:
            lines = "\n".join(f"• `{a}`" for a in hold_watchlist)
            await update.message.reply_text(f"📋 *Hold ({len(hold_watchlist)}):*\n{lines}", parse_mode=ParseMode.MARKDOWN)
        return

    if args[0] == "remove" and len(args) > 1:
        addr = args[1].strip()
        hold_watchlist.discard(addr)
        save_state()
        await update.message.reply_text(f"✅ Dihapus dari hold watchlist.", parse_mode=ParseMode.MARKDOWN)
        return

    if args[0] == "check":
        await update.message.reply_text("🔄 Mengecek kondisi hold positions...")
        await check_hold_watchlist(context.bot)
        return

    addr = args[0].strip()
    if len(addr) < 20:
        await update.message.reply_text("❌ Address tidak valid.")
        return
    if addr in hold_watchlist:
        await update.message.reply_text("⚠️ Address sudah di watchlist.")
        return

    await update.message.reply_text("⏳ Memvalidasi address...")
    pair = fetch_pair_by_address(addr)
    if not pair:
        await update.message.reply_text("❌ Tidak ditemukan di DexScreener.")
        return
    t = get_token_details(pair)
    if not t:
        await update.message.reply_text("❌ Gagal ambil data token.")
        return

    hold_watchlist.add(addr)
    save_state()
    await update.message.reply_text(
        f"✅ *Ditambahkan ke Hold Watchlist!*\n"
        f"🪙 {t['name']} (${t['symbol']})\n"
        f"💰 MCap: ${t['mcap']:,.0f}\n"
        f"Bot akan pantau setiap {config.CHECK_INTERVAL_MINUTES} menit.\n"
        f"Hapus: /hold remove {addr}",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /wallet add <address>    — tambah SM wallet
    /wallet remove <address> — hapus SM wallet
    /wallet list             — lihat semua
    /wallet check            — poll sekarang
    """
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args

    if not args or args[0] == "list":
        if not sm_wallets:
            await update.message.reply_text(
                "🧠 *Smart Money Wallet List* — Kosong\n\n"
                "Tambah wallet profitable via:\n/wallet add <address>\n\n"
                "Cari wallet dari:\n"
                "• gmgn.ai → leaderboard\n"
                "• nansen.ai → top Solana traders\n"
                "• dune.com → Solana Alpha Wallets dashboard",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            lines = "\n".join(f"{i+1}. `{w[:12]}...{w[-4:]}`" for i, w in enumerate(sm_wallets))
            await update.message.reply_text(
                f"🧠 *SM Wallets ({len(sm_wallets)}):*\n{lines}\n\n"
                f"Helius API: {'✅ OK' if config.HELIUS_API_KEY else '❌ Belum diset'}",
                parse_mode=ParseMode.MARKDOWN
            )
        return

    if args[0] == "add" and len(args) > 1:
        addr = args[1].strip()
        if len(addr) < 20:
            await update.message.reply_text("❌ Address tidak valid.")
            return
        if addr in sm_wallets:
            await update.message.reply_text("⚠️ Wallet sudah ada di list.")
            return
        sm_wallets.append(addr)
        save_state()
        await update.message.reply_text(
            f"✅ SM Wallet ditambahkan!\n`{addr[:12]}...{addr[-4:]}`\n\n"
            f"Total: {len(sm_wallets)} wallets\n"
            f"Bot akan alert setiap kali wallet ini beli token baru.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if args[0] == "remove" and len(args) > 1:
        addr = args[1].strip()
        if addr in sm_wallets:
            sm_wallets.remove(addr)
            save_state()
            await update.message.reply_text(f"✅ SM Wallet dihapus. Sisa: {len(sm_wallets)}")
        else:
            await update.message.reply_text("❌ Wallet tidak ditemukan di list.")
        return

    if args[0] == "check":
        if not config.HELIUS_API_KEY:
            await update.message.reply_text(
                "❌ HELIUS_API_KEY belum diset!\n\n"
                "Daftar gratis di: https://dev.helius.xyz/\n"
                "Lalu set di environment variable:\nHELIUS_API_KEY=your_key_here"
            )
            return
        if not sm_wallets:
            await update.message.reply_text("⚠️ Belum ada SM wallet. Tambah dulu: /wallet add <address>")
            return
        await update.message.reply_text(f"🔄 Polling {len(sm_wallets)} SM wallets...")
        await poll_smart_money_wallets(context.bot)
        return

    await update.message.reply_text(
        "Usage:\n/wallet add <address>\n/wallet remove <address>\n/wallet list\n/wallet check"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    now = time.time()
    active_seen = sum(1 for t in seen_addresses.values() if (now - t) / 3600 <= SEEN_TTL_HOURS)
    helius_ok   = "✅ OK" if config.HELIUS_API_KEY else "❌ Belum diset (daftar di dev.helius.xyz)"
    await update.message.reply_text(
        f"✅ *ONLINE* | v6\n━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Scan interval: {config.CHECK_INTERVAL_MINUTES} menit\n"
        f"🔁 Seen aktif: {active_seen} (TTL {SEEN_TTL_HOURS}j)\n"
        f"📋 Hold watchlist: {len(hold_watchlist)} koin\n"
        f"🧠 SM wallets: {len(sm_wallets)}\n"
        f"📡 Helius API: {helius_ok}\n"
        f"📈 Vol history: {len(volume_history)} token\n"
        f"💾 State file: {'✅' if os.path.exists(STATE_FILE) else '❌'}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"🌙≥{config.SCORE_MOONBAG} | 🎯≥{config.SCORE_SWING} | ⚡≥{config.SCORE_SCALP}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await update.message.reply_text(
        f"🔍 *Filter Aktif v6*\n━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Min Liq: ${config.MIN_LIQUIDITY_USD:,}\n"
        f"📊 Min Vol 1h: $1,000\n"
        f"⏰ Max Age: {config.MAX_AGE_HOURS} jam\n"
        f"🛡️ Anti wash trading: ON\n"
        f"🚫 Anti copycat/rekt: ON\n"
        f"📉 Anti distribusi: ON\n"
        f"📉 Anti downtrend 6h: ON\n"
        f"⏳ Dedup TTL: {SEEN_TTL_HOURS} jam\n"
        f"🧠 Smart money check: {'ON' if config.ENABLE_SMART_MONEY_CHECK else 'OFF'}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    n = len(seen_addresses)
    seen_addresses.clear()
    save_state()
    await update.message.reply_text(
        f"✅ Cache di-reset ({n} entri dihapus).\n"
        f"Hold watchlist & SM wallets tetap aman."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ══════════════════════════════════════════════════════════════
#  BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════

async def background_scanner(bot):
    """Auto scan token setiap CHECK_INTERVAL_MINUTES."""
    while True:
        await asyncio.sleep(config.CHECK_INTERVAL_MINUTES * 60)
        await do_scan(bot, manual=False)

async def background_wallet_poller(bot):
    """Poll SM wallets lebih sering dari token scan."""
    await asyncio.sleep(60)  # tunggu 1 menit setelah startup
    while True:
        if sm_wallets and config.HELIUS_API_KEY:
            await poll_smart_money_wallets(bot)
        await asyncio.sleep(config.WALLET_POLL_INTERVAL_MIN * 60)


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def main():
    print("=" * 50)
    print("  MEMECOIN MONITOR v6")
    print("=" * 50)

    load_state()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for cmd, handler in [
        ("start",      cmd_start),
        ("scan",       cmd_scan),
        ("hold",       cmd_hold),
        ("wallet",     cmd_wallet),
        ("status",     cmd_status),
        ("filter",     cmd_filter),
        ("clearcache", cmd_clearcache),
        ("help",       cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    async with app:
        await app.start()
        bot = app.bot

        helius_status = "✅ Helius OK" if config.HELIUS_API_KEY else "⚠️ Helius belum diset — /wallet tidak aktif"
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                "🤖 *MEMECOIN MONITOR v6 AKTIF*\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "✅ Anti-distribusi filter\n"
                "✅ Fresh Graduate detection\n"
                "✅ Volume Resurrection\n"
                "✅ TTL-based dedup (6 jam)\n"
                "✅ Persistent state\n"
                "✅ Hold Watchlist monitoring\n"
                "✅ Smart Money wallet tracking\n"
                "✅ Fixed scan summary (skor rendah kini tampil)\n"
                f"{helius_status}\n"
                f"🧠 SM Wallets loaded: {len(sm_wallets)}\n\n"
                "/scan — scan manual\n"
                "/wallet add <address> — tambah SM wallet\n"
                "/hold <address> — pantau koin hold\n"
                "⏳ Auto scan pertama dimulai..."
            ),
            parse_mode=ParseMode.MARKDOWN
        )

        await do_scan(bot, manual=False)
        await app.updater.start_polling(drop_pending_updates=True)

        # Jalankan dua background task sekaligus
        await asyncio.gather(
            background_scanner(bot),
            background_wallet_poller(bot),
        )


if __name__ == "__main__":
    asyncio.run(main())
