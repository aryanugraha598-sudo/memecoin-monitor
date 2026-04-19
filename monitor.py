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
volume_history = {}    # token_address -> [(timestamp, vol_1h)]
seen_addresses = {}    # token_address -> timestamp (TTL-based)
hold_watchlist = set() # token address yang user hold
sm_wallets     = []    # manual SM wallet list (dari config + /wallet add)
sm_last_buy    = {}    # wallet -> {"token": addr, "time": ts}

SEEN_TTL_HOURS = 6
STATE_FILE     = "state.json"


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
        for w in data.get("sm_wallets", []):
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
#  VOLUME TRACKING & RESURRECTION
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
#  GMGN SMART MONEY — AUTO DETECT (tanpa API key)
# ══════════════════════════════════════════════════════════════

GMGN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://gmgn.ai/",
}

def gmgn_get_top_traders(token_address, limit=20):
    """
    Ambil top traders untuk token ini dari GMGN public API.
    Return: list of trader dicts dengan field: wallet, realized_profit, tags, etc.
    Gratis, tanpa API key.
    """
    try:
        url = f"https://gmgn.ai/defi/quotation/v1/tokens/top_traders/sol/{token_address}"
        r   = requests.get(url, headers=GMGN_HEADERS, params={"limit": limit}, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
        # Response: {"code":0, "data": {"items": [...]}}
        items = data.get("data", {}).get("items", [])
        return items
    except Exception as e:
        print(f"  GMGN top_traders err: {e}")
        return []


def gmgn_check_smart_money(token_address, token_name=""):
    """
    Analisis top traders token ini di GMGN.
    Return: (score_bonus, summary_text, insider_wallets_found)
    """
    if not config.ENABLE_GMGN_SMART_MONEY:
        return 0, None, []

    traders = gmgn_get_top_traders(token_address)
    if not traders:
        return 0, None, []

    total_bonus    = 0
    found_labels   = []
    insider_wallets = []

    for trader in traders:
        tags = trader.get("tags", []) or []
        # Konversi ke lowercase untuk matching
        tags_lower = [str(t).lower() for t in tags]

        # Cek setiap label di config
        for label_key, bonus in config.GMGN_LABEL_SCORES.items():
            if any(label_key in tag for tag in tags_lower):
                total_bonus += bonus
                found_labels.append(label_key)
                wallet = trader.get("address", "")
                if wallet:
                    insider_wallets.append({
                        "wallet": wallet,
                        "label":  label_key,
                        "pnl":    trader.get("realized_profit", 0),
                    })
                break  # satu trader hanya dihitung sekali

    # Cap bonus
    total_bonus = min(total_bonus, config.GMGN_MAX_BONUS)

    if not found_labels:
        return 0, None, []

    # Format summary
    label_counts = {}
    for l in found_labels:
        label_counts[l] = label_counts.get(l, 0) + 1

    parts = []
    emoji_map = {
        "insider":     "🔴 Insider",
        "smart_degen": "🧠 Smart Degen",
        "kol":         "📢 KOL",
        "sniper":      "🎯 Sniper",
        "whale":       "🐋 Whale",
        "smart":       "🧠 Smart",
    }
    for label, count in label_counts.items():
        parts.append(f"{emoji_map.get(label, label.title())} x{count}")

    summary = "👤 " + " | ".join(parts) + f" (+{total_bonus} pts)"

    if found_labels:
        print(f"  🧠 GMGN SM in {token_name}: {', '.join(found_labels)}")

    return total_bonus, summary, insider_wallets


def gmgn_get_token_info(token_address):
    """
    Ambil info tambahan token dari GMGN: renounced, top10 holder, dll.
    Bonus: sinyal keamanan tambahan.
    """
    try:
        url = f"https://gmgn.ai/api/v1/token_info/sol/{token_address}"
        r   = requests.get(url, headers=GMGN_HEADERS, timeout=8)
        if r.status_code != 200:
            return {}
        return r.json().get("data", {})
    except:
        return {}


# ══════════════════════════════════════════════════════════════
#  HELIUS (opsional — untuk manual SM wallet polling)
# ══════════════════════════════════════════════════════════════

def helius_get(endpoint, params=None):
    if not config.HELIUS_API_KEY:
        return None
    try:
        p = params or {}
        p["api-key"] = config.HELIUS_API_KEY
        r = requests.get(f"https://api.helius.xyz/v0/{endpoint}", params=p, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  Helius err: {e}")
    return None

def get_wallet_recent_buys(wallet_address, limit=5):
    if not config.HELIUS_API_KEY:
        return []
    data = helius_get(f"addresses/{wallet_address}/transactions",
                      {"type": "SWAP", "limit": limit})
    if not data:
        return []
    recent = []
    for tx in data:
        ts = tx.get("timestamp", 0)
        if time.time() - ts > 1800:
            continue
        swap = tx.get("events", {}).get("swap", {})
        for t in swap.get("tokenOutputs", []):
            mint = t.get("mint", "")
            if mint and mint != "So11111111111111111111111111111111111111112":
                recent.append({"address": mint, "wallet": wallet_address,
                                "timestamp": ts})
    return recent


# ══════════════════════════════════════════════════════════════
#  NITTER — TWITTER MENTIONS
# ══════════════════════════════════════════════════════════════

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.lucahammer.com",
]

def search_twitter_mentions(symbol, address):
    headers = {"User-Agent": "Mozilla/5.0"}
    tweets  = []
    for query in [f"${symbol}", address[:20]]:
        for instance in NITTER_INSTANCES:
            try:
                url = f"{instance}/search?q={requests.utils.quote(query)}&f=tweets"
                r   = requests.get(url, headers=headers, timeout=6)
                if r.status_code != 200:
                    continue
                for m in re.findall(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>', r.text, re.DOTALL)[:4]:
                    clean = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', m)).strip()
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
#  DATA FETCHING & PAIR DEDUPLICATION
# ══════════════════════════════════════════════════════════════

def fetch_token_best_pair(token_addr):
    """Ambil pair terbaik (likuiditas tertinggi) untuk satu token address."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8
        )
        if r.status_code != 200:
            return None, []
        all_pairs = r.json().get("pairs", [])
        sol_pairs = [p for p in all_pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None, []

        # Urutkan dari likuiditas tertinggi
        sol_pairs.sort(
            key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True
        )
        best = sol_pairs[0]
        # Kembalikan best pair + semua pair URL untuk ditampilkan di alert
        all_urls = [(p.get("dexId", "?"), p.get("url", ""),
                     float(p.get("liquidity", {}).get("usd", 0) or 0))
                    for p in sol_pairs]
        return best, all_urls
    except:
        return None, []


def get_solana_pairs():
    """
    Fetch semua kandidat pair dari DexScreener.
    Dedup by token address — kalau satu token punya banyak pair,
    ambil yang liquidity tertinggi, simpan semua URL-nya.
    """
    token_map  = {}  # token_address -> (best_pair, all_urls)
    seen_token = set()

    def process_token(token_addr):
        if not token_addr or token_addr in seen_token:
            return
        seen_token.add(token_addr)
        best, all_urls = fetch_token_best_pair(token_addr)
        if best:
            token_map[token_addr] = (best, all_urls)

    # EP1: Token profiles (pump.fun graduates)
    try:
        r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        if r.status_code == 200:
            tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Profiles: {len(tokens)}")
            for t in tokens[:25]:
                process_token(t.get("tokenAddress", ""))
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
                process_token(t.get("tokenAddress", ""))
                time.sleep(0.15)
    except Exception as e:
        print(f"  EP2 err: {e}")

    # EP3 & EP4: Direct search — dedup inline
    for query in ["pump sol", "raydium"]:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={query}", timeout=10)
            if r.status_code == 200:
                for p in r.json().get("pairs", []):
                    if p.get("chainId") == "solana":
                        addr = p.get("baseToken", {}).get("address", "")
                        if addr and addr not in seen_token:
                            seen_token.add(addr)
                            # Untuk EP3/EP4, kita punya pair-nya langsung
                            # Cek apakah sudah ada yang lebih likuid
                            existing = token_map.get(addr)
                            cur_liq  = float(p.get("liquidity", {}).get("usd", 0) or 0)
                            if not existing or cur_liq > float(
                                    existing[0].get("liquidity", {}).get("usd", 0) or 0):
                                token_map[addr] = (p, [(p.get("dexId","?"), p.get("url",""), cur_liq)])
        except Exception as e:
            print(f"  EP search err ({query}): {e}")

    result = list(token_map.values())
    print(f"  Total unique tokens: {len(result)}")
    return result  # list of (best_pair, all_urls)


def get_token_details(pair, all_urls=None):
    """Parse pair data menjadi dict yang mudah dipakai."""
    try:
        base = pair.get("baseToken", {})
        liq  = pair.get("liquidity", {})
        vol  = pair.get("volume", {})
        pc   = pair.get("priceChange", {})
        txns = pair.get("txns", {})

        created_at = pair.get("pairCreatedAt", 0)
        age_h = (time.time() - created_at / 1000) / 3600 if created_at else 9999

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

        avg_6h = v6h / 6 if v6h > 0 else 0
        vol_accel = round(v1h / avg_6h, 2) if avg_6h > 50 else 0

        wash = 0; abpm = 0
        if makers > 0 and h24_buys > 0:
            abpm = h24_buys / makers
            if abpm > 6:     wash = 3
            elif abpm > 4:   wash = 2
            elif abpm > 2.5: wash = 1

        # Format semua pair URL untuk ditampilkan
        pair_urls_str = ""
        if all_urls and len(all_urls) > 1:
            pair_urls_str = " | ".join(
                f"[{dex}](${url})" for dex, url, _ in all_urls[:3]
            )

        return {
            "name":          base.get("name", "Unknown"),
            "symbol":        base.get("symbol", "???"),
            "address":       base.get("address", ""),   # ← token contract address
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
            "pair_url":      pair.get("url", ""),
            "all_pair_urls": all_urls or [],   # semua pair yang ada
            "dex_id":        pair.get("dexId", ""),
        }
    except Exception as e:
        print(f"  Parse err: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  FILTER
# ══════════════════════════════════════════════════════════════

def passes_filter(token):
    if token["mcap"] <= 0:
        return False, "MCap invalid"
    if token["liquidity_usd"] < config.MIN_LIQUIDITY_USD:
        return False, "Liquidity rendah"
    if token["wash"] >= 3:
        return False, f"Wash trading ({token['abpm']}x/wallet)"
    if token["v1h"] < config.MIN_VOLUME_1H:
        return False, f"Vol 1h < ${config.MIN_VOLUME_1H:,}"

    # Anti distribusi: harga turun + volume melambat
    if token["pc_1h"] < -5 and token["vol_accel"] < 1.0:
        return False, f"Distribusi: -{abs(token['pc_1h'])}% + accel {token['vol_accel']}x"

    # Anti downtrend konsisten
    if token["pc_1h"] < -10 and token["pc_6h"] < -15:
        return False, f"Downtrend: 1h={token['pc_1h']}% 6h={token['pc_6h']}%"

    # Volume manipulasi ekstrem
    if token["v24h"] > 0 and token["mcap"] > 0:
        if token["v24h"] / token["mcap"] > 100:
            return False, "Vol/MCap >100x"

    # ✅ Tidak ada BLOCK_NAMES lagi — semua nama boleh lolos
    # ✅ Tidak ada MAX_AGE_HOURS lagi — umur hanya mempengaruhi score

    return True, "OK"


# ══════════════════════════════════════════════════════════════
#  SCORING (dengan age modifier)
# ══════════════════════════════════════════════════════════════

def score_token(token):
    score    = 0
    reasons  = []
    warnings = []
    age      = token["age_h"]

    is_res, res_mult = detect_resurrection(token["address"], token["v1h"])

    # ── Tentukan tipe ─────────────────────────────────────────
    if age < config.AGE_TIER_FRESH and token["v1h"] > 2000:
        ttype = "FRESH_GRADUATE"
    elif is_res:
        ttype = "RESURRECTION"
    elif token["pc_1h"] > 15 and token["h1_bsr"] > 1.5:
        ttype = "MOMENTUM"
    elif token["vol_accel"] > 2 and token["h1_bsr"] > 1.3:
        ttype = "ACCUMULATION"
    else:
        ttype = "NORMAL"

    # ── Age modifier ──────────────────────────────────────────
    # Koin tua tidak langsung dibuang, tapi perlu sinyal lebih kuat
    if age < config.AGE_TIER_FRESH:
        age_mod = 0       # fresh — tidak ada penalty
    elif age < config.AGE_TIER_YOUNG:
        age_mod = -5      # 6-24 jam
    elif age < config.AGE_TIER_NORMAL:
        age_mod = -10     # 1-7 hari
    elif age < config.AGE_TIER_OLD:
        age_mod = -20     # 7-30 hari
    else:
        age_mod = -35     # 30+ hari — butuh resurrection sangat kuat
        warnings.append(f"⏰ Token tua ({round(age/24,1)} hari) — perlu konfirmasi kuat")

    # EXCEPTION: kalau RESURRECTION, age penalty dikurangi drastis
    if ttype == "RESURRECTION":
        age_mod = max(age_mod // 3, -5)

    score += age_mod

    # ── Score by type ─────────────────────────────────────────
    if ttype == "FRESH_GRADUATE":
        score += 20; reasons.append(f"🆕 Fresh graduate ({round(age,1)} jam)")
        if token["h1_bsr"] > 3:     score += 30; reasons.append(f"💚 Buy pressure kuat ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 2:   score += 20; reasons.append(f"✅ Buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.3: score += 10; reasons.append(f"🟡 Buy moderate ({token['h1_bsr']}x)")
        if token["v1h"] > 50000:    score += 20; reasons.append(f"🔥 Vol 1h: ${token['v1h']:,.0f}")
        elif token["v1h"] > 10000:  score += 12; reasons.append(f"📈 Vol 1h: ${token['v1h']:,.0f}")
        elif token["v1h"] > 3000:   score += 5
        if token["mcap"] < 100000:  score += 15; reasons.append(f"💰 MCap kecil (${token['mcap']:,.0f})")
        elif token["mcap"] < 500000:score += 8;  reasons.append(f"💰 MCap (${token['mcap']:,.0f})")

    elif ttype == "RESURRECTION":
        score += 35; reasons.append(f"⚡ RESURRECTION! Volume {res_mult}x dari baseline ({round(age/24,1)} hari lalu)")
        if token["h1_bsr"] > 2:    score += 25; reasons.append(f"💚 Buy pressure ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.3:score += 15; reasons.append(f"🟡 Buy ada ({token['h1_bsr']}x)")
        if token["pc_1h"] > 50:    score += 25; reasons.append(f"🚀 +{token['pc_1h']}% dalam 1 jam!")
        elif token["pc_1h"] > 20:  score += 12; reasons.append(f"📈 +{token['pc_1h']}%")
        if token["v1h"] > 20000:   score += 15; reasons.append(f"🔥 Vol 1h: ${token['v1h']:,.0f}")

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
        elif token["h1_bsr"] > 1.5: score += 12; reasons.append(f"🟡 Buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] < 0.7: score -= 20; warnings.append("🔴 Sell pressure tinggi!")

    # ── Universal ─────────────────────────────────────────────
    if token["mcap"] > 0:
        lr = token["liquidity_usd"] / token["mcap"]
        if lr > 0.15:    score += 10; reasons.append(f"💧 Liquidity sehat ({round(lr*100,1)}%)")
        elif lr > 0.05:  score += 5
        elif lr < 0.02:  score -= 10; warnings.append("⚠️ Liquidity tipis!")

    if token["wash"] == 2:   score -= 15; warnings.append(f"🔶 Wash trading ({token['abpm']}x/wallet)")
    elif token["wash"] == 1: score -= 5;  warnings.append(f"⚠️ Suspicious ({token['abpm']}x/wallet)")
    if token["pc_1h"] < -25: score -= 20; warnings.append(f"📉 Dump -{abs(token['pc_1h'])}% 1h!")
    if token["pc_5m"] > 5:   score += 5;  reasons.append(f"🟢 +{token['pc_5m']}% dalam 5m")

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
#  TELEGRAM ALERT
# ══════════════════════════════════════════════════════════════

def fmt_age(age_h):
    if age_h < 24:
        return f"{round(age_h,1)} jam"
    elif age_h < 24*30:
        return f"{round(age_h/24,1)} hari"
    else:
        return f"{round(age_h/24/30,1)} bulan"

async def send_alert(bot, token, score, sig, desc,
                     reasons, warnings, ttype,
                     tw=None, gmgn_summary=None, insider_wallets=None):

    wash_l  = ["✅ Organik","🟡 Suspicious","🔶 High Suspicious","🔴 Wash"][min(token["wash"],3)]
    ttype_e = {"FRESH_GRADUATE":"🆕","RESURRECTION":"⚡","MOMENTUM":"🚀","ACCUMULATION":"📈"}.get(ttype,"📊")

    # ── Smart money section ───────────────────────────────────
    sm_section = ""
    if gmgn_summary:
        sm_section = f"\n🧠 *Smart Money (GMGN):*\n{gmgn_summary}\n"

    # ── Multi-pair section ────────────────────────────────────
    pair_section = ""
    if len(token["all_pair_urls"]) > 1:
        pair_lines = []
        for i, (dex, url, liq) in enumerate(token["all_pair_urls"][:4]):
            marker = "★" if i == 0 else "  "
            pair_lines.append(f"{marker} [{dex}]({url}) (Liq: ${liq:,.0f})")
        pair_section = "\n🔀 *Semua Pair:*\n" + "\n".join(pair_lines) + "\n"

    # ── Insider wallets ───────────────────────────────────────
    insider_section = ""
    if insider_wallets:
        top3 = insider_wallets[:3]
        lines = []
        for iw in top3:
            short = iw["wallet"][:6] + "..." + iw["wallet"][-4:]
            pnl   = iw.get("pnl", 0)
            pnl_s = f"+${pnl:,.0f}" if pnl > 0 else f"-${abs(pnl):,.0f}"
            lines.append(f"  • `{short}` [{iw['label'].title()}] PNL: {pnl_s}")
        insider_section = "\n💼 *Insider Wallets:*\n" + "\n".join(lines) + "\n"

    msg = (
        f"{sig}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token['name']}* (${token['symbol']})\n"
        f"{ttype_e} Type: *{ttype}*  |  📊 Score: *{score}/100*\n"
        f"💡 {desc}\n"
        f"{sm_section}"
        f"\n📋 *Token Info*\n"
        f"├ CA: `{token['address']}`\n"   # ← contract address, bisa di-copy
        f"├ DEX: {token['dex_id']}\n"
        f"├ Age: {fmt_age(token['age_h'])}\n\n"
        f"📈 *Market Data*\n"
        f"├ MCap: ${token['mcap']:,.0f}\n"
        f"├ Liquidity: ${token['liquidity_usd']:,.0f}\n"
        f"├ Vol 1h: ${token['v1h']:,.0f}\n"
        f"├ Vol 24h: ${token['v24h']:,.0f}\n"
        f"├ Buy/Sell 1h: {token['h1_bsr']}x\n"
        f"├ Vol Accel: {token['vol_accel']}x\n"
        f"├ Trading: {wash_l}\n"
        f"└ 5m: {token['pc_5m']}% | 1h: {token['pc_1h']}% | 6h: {token['pc_6h']}%\n"
        f"{pair_section}"
        f"{insider_section}"
        f"\n✅ *Kenapa Menarik:*\n" + "\n".join(reasons)
    )
    if warnings:
        msg += "\n\n⚠️ *Warning:*\n" + "\n".join(warnings)
    if tw:
        msg += f"\n\n{tw}"

    msg += f"\n\n🔗 [Chart & Trade]({token['pair_url']})"
    msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{token['address']})"
    msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ══════════════════════════════════════════════════════════════
#  HOLD WATCHLIST
# ══════════════════════════════════════════════════════════════

def fetch_pair_by_address(address):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=8)
        if r.status_code != 200:
            return None, []
        pairs     = r.json().get("pairs", [])
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None, []
        sol_pairs.sort(
            key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True
        )
        all_urls = [(p.get("dexId","?"), p.get("url",""),
                     float(p.get("liquidity",{}).get("usd",0) or 0))
                    for p in sol_pairs]
        return sol_pairs[0], all_urls
    except:
        return None, []

async def check_hold_watchlist(bot):
    if not hold_watchlist:
        return
    print(f"  Checking {len(hold_watchlist)} hold positions...")
    for address in list(hold_watchlist):
        try:
            pair, all_urls = fetch_pair_by_address(address)
            if not pair:
                continue
            t = get_token_details(pair, all_urls)
            if not t:
                continue

            track_volume(t["address"], t["v1h"])
            score, reasons, warnings, ttype = score_token(t)

            # GMGN check untuk hold juga
            gmgn_bonus, gmgn_summary, insider_wallets = gmgn_check_smart_money(address, t["name"])
            score += gmgn_bonus

            if score >= 60:   se, st = "🟢", "Kondisi BAGUS — hold lanjut"
            elif score >= 40: se, st = "🟡", "Kondisi MODERAT — pantau ketat"
            else:             se, st = "🔴", "Kondisi MELEMAH — pertimbangkan EXIT!"

            pair_section = ""
            if len(all_urls) > 1:
                pair_lines = [f"{'★' if i==0 else ' '} [{d}]({u}) Liq:${l:,.0f}"
                              for i, (d, u, l) in enumerate(all_urls[:3])]
                pair_section = "\n🔀 *Semua Pair:*\n" + "\n".join(pair_lines)

            hold_msg = (
                f"📋 *HOLD MONITOR UPDATE*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{t['name']}* (${t['symbol']})\n"
                f"{se} Score: *{score}/100* — {st}\n\n"
                f"📋 CA: `{t['address']}`\n\n"
                f"📈 5m: {t['pc_5m']}% | 1h: {t['pc_1h']}% | 6h: {t['pc_6h']}%\n"
                f"├ Vol 1h: ${t['v1h']:,.0f}\n"
                f"├ Vol Accel: {t['vol_accel']}x\n"
                f"├ Buy/Sell 1h: {t['h1_bsr']}x\n"
                f"├ Liquidity: ${t['liquidity_usd']:,.0f}\n"
                f"└ MCap: ${t['mcap']:,.0f}\n"
                f"{pair_section}"
            )
            if gmgn_summary:
                hold_msg += f"\n\n🧠 GMGN: {gmgn_summary}"
            if score < 30:
                hold_msg += "\n\n⛔ *WARNING: Kondisi sangat lemah — pertimbangkan EXIT!*"
            elif warnings:
                hold_msg += "\n\n⚠️ *Warning:*\n" + "\n".join(warnings)

            hold_msg += f"\n\n🔗 [Chart]({t['pair_url']}) | [GMGN](https://gmgn.ai/sol/token/{t['address']})"
            hold_msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
            hold_msg += f"\n_Hapus: /hold remove {address}_"

            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=hold_msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  Hold err {address[:8]}: {e}")


# ══════════════════════════════════════════════════════════════
#  SMART MONEY WALLET POLLER (Helius)
# ══════════════════════════════════════════════════════════════

async def poll_smart_money_wallets(bot):
    if not sm_wallets or not config.HELIUS_API_KEY:
        return
    print(f"  Polling {len(sm_wallets)} SM wallets...")
    for wallet in sm_wallets:
        try:
            recent = get_wallet_recent_buys(wallet, limit=5)
            for buy in recent:
                token_addr = buy["address"]
                prev = sm_last_buy.get(wallet, {})
                if prev.get("token") == token_addr:
                    continue
                sm_last_buy[wallet] = {"token": token_addr, "time": buy["timestamp"]}

                pair, all_urls = fetch_pair_by_address(token_addr)
                if not pair:
                    continue
                t = get_token_details(pair, all_urls)
                if not t:
                    continue

                score, reasons, warnings, ttype = score_token(t)
                gmgn_bonus, gmgn_summary, _ = gmgn_check_smart_money(token_addr, t["name"])
                score += gmgn_bonus

                wallet_short = wallet[:6] + "..." + wallet[-4:]
                ts_str = datetime.fromtimestamp(buy["timestamp"]).strftime('%H:%M:%S')

                alert_msg = (
                    f"🧠 *SMART MONEY ALERT*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"💼 `{wallet_short}` baru saja beli!\n"
                    f"⏰ {ts_str} WIB\n\n"
                    f"🪙 *{t['name']}* (${t['symbol']})\n"
                    f"📋 CA: `{t['address']}`\n"
                    f"📊 Score: *{score}/100*\n\n"
                    f"├ MCap: ${t['mcap']:,.0f}\n"
                    f"├ Age: {fmt_age(t['age_h'])}\n"
                    f"├ Vol 1h: ${t['v1h']:,.0f}\n"
                    f"└ Buy/Sell 1h: {t['h1_bsr']}x\n"
                )
                if gmgn_summary:
                    alert_msg += f"\n🧠 GMGN: {gmgn_summary}"
                if warnings:
                    alert_msg += "\n\n⚠️ " + " | ".join(warnings)
                alert_msg += f"\n\n🔗 [Chart]({t['pair_url']}) | [GMGN](https://gmgn.ai/sol/token/{t['address']})"
                alert_msg += f"\n_/hold {token_addr}_"

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
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=f"🔄 *Manual Scan Dimulai* ({ts} WIB)\n⏳ Sedang memindai...",
            parse_mode=ParseMode.MARKDOWN
        )

    token_pairs = get_solana_pairs()   # list of (best_pair, all_urls)
    sent         = 0
    filtered     = 0
    low_score    = 0
    already_seen = 0

    for best_pair, all_urls in token_pairs:
        t = get_token_details(best_pair, all_urls)
        if not t or not t["address"]:
            continue

        track_volume(t["address"], t["v1h"])

        if is_seen(t["address"]):
            already_seen += 1
            continue

        ok, reason = passes_filter(t)
        if not ok:
            print(f"  ✗ {t['name'][:20]:<20} {reason}")
            filtered += 1
            continue

        score, reasons, warnings, ttype = score_token(t)
        sig, desc = get_signal(score, ttype)

        multi = f" [{len(all_urls)} pairs]" if len(all_urls) > 1 else ""
        print(f"  {ttype[:4]} | {t['name'][:15]:<15} | S:{score:>3} | MC:${t['mcap']:>9,.0f} | V1h:${t['v1h']:>8,.0f} | BSR:{t['h1_bsr']}{multi}")

        if not sig:
            low_score += 1
            print(f"         └─ Skor rendah ({score}), tidak di-alert")
            continue

        mark_seen(t["address"])

        # ── GMGN Smart Money Check ────────────────────────────
        gmgn_bonus, gmgn_summary, insider_wallets = 0, None, []
        if config.ENABLE_GMGN_SMART_MONEY:
            gmgn_bonus, gmgn_summary, insider_wallets = gmgn_check_smart_money(
                t["address"], t["name"]
            )
            if gmgn_bonus > 0:
                score += gmgn_bonus
                reasons.append(f"🧠 Smart Money GMGN detected!")
                # Recalculate signal setelah bonus
                sig, desc = get_signal(score, ttype)

        # ── Twitter Check ─────────────────────────────────────
        tw_bonus, tw_summary = 0, None
        if score >= 55 and config.ENABLE_TWITTER_CHECK:
            tw_bonus, tw_summary = analyze_twitter(t["name"], t["symbol"], t["address"])
            score += tw_bonus

        print(f"  >>> {sig} (score={score}){' 🧠SM' if gmgn_bonus > 0 else ''}")
        await send_alert(bot, t, score, sig, desc, reasons, warnings, ttype,
                         tw_summary, gmgn_summary, insider_wallets)
        sent += 1
        await asyncio.sleep(1.5)

    await check_hold_watchlist(bot)

    if manual:
        hold_note = f"\n📋 Hold watchlist: {len(hold_watchlist)} koin" if hold_watchlist else ""
        sm_note   = f"\n🧠 SM wallets: {len(sm_wallets)}" if sm_wallets else ""
        gmgn_note = "\n🔍 GMGN smart money check: ✅ aktif" if config.ENABLE_GMGN_SMART_MONEY else ""
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"✅ *Scan Selesai*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Dipindai: {len(token_pairs)} token unik\n"
                f"🔁 Sudah seen (TTL): {already_seen}\n"
                f"🚫 Difilter: {filtered}\n"
                f"📉 Skor rendah: {low_score}\n"
                f"🔔 Alert: {sent}\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
                f"{hold_note}{sm_note}{gmgn_note}"
                + ("\n\n💤 Belum ada koin memenuhi kriteria." if sent == 0 else "")
            ),
            parse_mode=ParseMode.MARKDOWN
        )

    cleanup_seen()
    if len(volume_history) > 2000:
        oldest = sorted(volume_history, key=lambda k: volume_history[k][-1][0])[:500]
        for k in oldest:
            del volume_history[k]

    save_state()
    print(f"  Done: {sent} alerts | {filtered} filtered | {low_score} low | {already_seen} seen")


# ══════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v7*\n━━━━━━━━━━━━━━━━━━━\n"
        "*/scan* — Scan manual\n"
        "*/hold <address>* — Pantau koin hold\n"
        "*/hold list* — Lihat hold watchlist\n"
        "*/hold remove <address>* — Hapus dari hold\n"
        "*/hold check* — Cek kondisi hold sekarang\n"
        "*/wallet add <address>* — Tambah SM wallet (Helius)\n"
        "*/wallet list* — Lihat SM wallet list\n"
        "*/wallet check* — Poll SM wallets sekarang\n"
        "*/gmgn <address>* — Cek token di GMGN manual\n"
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
            "/hold <address> | /hold list | /hold remove <addr> | /hold check",
            parse_mode=ParseMode.MARKDOWN)
        return

    if args[0] == "list":
        if not hold_watchlist:
            await update.message.reply_text("📋 Watchlist kosong.")
        else:
            lines = "\n".join(f"• `{a}`" for a in hold_watchlist)
            await update.message.reply_text(f"📋 *Hold ({len(hold_watchlist)}):*\n{lines}", parse_mode=ParseMode.MARKDOWN)
        return

    if args[0] == "remove" and len(args) > 1:
        hold_watchlist.discard(args[1].strip())
        save_state()
        await update.message.reply_text("✅ Dihapus dari hold watchlist.")
        return

    if args[0] == "check":
        await update.message.reply_text("🔄 Mengecek hold positions...")
        await check_hold_watchlist(context.bot)
        return

    addr = args[0].strip()
    if len(addr) < 20:
        await update.message.reply_text("❌ Address tidak valid.")
        return
    if addr in hold_watchlist:
        await update.message.reply_text("⚠️ Sudah ada di watchlist.")
        return

    await update.message.reply_text("⏳ Validasi address...")
    pair, all_urls = fetch_pair_by_address(addr)
    if not pair:
        await update.message.reply_text("❌ Tidak ditemukan di DexScreener.")
        return
    t = get_token_details(pair, all_urls)
    if not t:
        await update.message.reply_text("❌ Gagal ambil data token.")
        return

    hold_watchlist.add(addr)
    save_state()

    multi = f" ({len(all_urls)} pairs)" if len(all_urls) > 1 else ""
    await update.message.reply_text(
        f"✅ *Ditambahkan ke Hold Watchlist!*\n"
        f"🪙 {t['name']} (${t['symbol']}){multi}\n"
        f"📋 CA: `{addr}`\n"
        f"💰 MCap: ${t['mcap']:,.0f} | Liq: ${t['liquidity_usd']:,.0f}\n"
        f"Bot pantau setiap {config.CHECK_INTERVAL_MINUTES} menit.",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args

    if not args or args[0] == "list":
        if not sm_wallets:
            await update.message.reply_text(
                "🧠 *SM Wallet List* — Kosong\n\n"
                "Catatan: GMGN smart money check sudah otomatis per token.\n"
                "Wallet manual di sini untuk real-time polling via Helius.\n\n"
                "/wallet add <address>",
                parse_mode=ParseMode.MARKDOWN)
        else:
            lines = "\n".join(f"{i+1}. `{w[:12]}...{w[-4:]}`" for i, w in enumerate(sm_wallets))
            await update.message.reply_text(
                f"🧠 *SM Wallets ({len(sm_wallets)}):*\n{lines}\n\n"
                f"Helius: {'✅' if config.HELIUS_API_KEY else '❌ belum diset'}",
                parse_mode=ParseMode.MARKDOWN)
        return

    if args[0] == "add" and len(args) > 1:
        addr = args[1].strip()
        if addr in sm_wallets:
            await update.message.reply_text("⚠️ Sudah ada.")
            return
        sm_wallets.append(addr)
        save_state()
        await update.message.reply_text(f"✅ SM Wallet ditambahkan! Total: {len(sm_wallets)}")
        return

    if args[0] == "remove" and len(args) > 1:
        addr = args[1].strip()
        if addr in sm_wallets:
            sm_wallets.remove(addr)
            save_state()
            await update.message.reply_text(f"✅ Dihapus. Sisa: {len(sm_wallets)}")
        else:
            await update.message.reply_text("❌ Tidak ditemukan.")
        return

    if args[0] == "check":
        if not config.HELIUS_API_KEY:
            await update.message.reply_text(
                "❌ HELIUS_API_KEY belum diset!\nDaftar gratis: https://dev.helius.xyz/")
            return
        await update.message.reply_text(f"🔄 Polling {len(sm_wallets)} wallets...")
        await poll_smart_money_wallets(context.bot)
        return

async def cmd_gmgn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /gmgn <token_address> — Manual cek smart money token di GMGN
    """
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /gmgn <token_address>")
        return

    addr = args[0].strip()
    await update.message.reply_text(f"🔍 Mengecek GMGN untuk token...\n`{addr}`", parse_mode=ParseMode.MARKDOWN)

    bonus, summary, insiders = gmgn_check_smart_money(addr)
    traders = gmgn_get_top_traders(addr, limit=10)

    if not traders:
        await update.message.reply_text("❌ Tidak ada data top traders dari GMGN untuk token ini.")
        return

    msg = f"🧠 *GMGN Smart Money Report*\n━━━━━━━━━━━━━━━━━━━\n"
    msg += f"Score bonus: +{bonus} pts\n"
    if summary:
        msg += f"{summary}\n"
    msg += f"\n👤 *Top Traders ({len(traders)}):*\n"

    for i, tr in enumerate(traders[:8]):
        wallet = tr.get("address", "")
        short  = wallet[:6] + "..." + wallet[-4:] if wallet else "?"
        pnl    = tr.get("realized_profit", 0) or 0
        tags   = ", ".join(str(t) for t in (tr.get("tags") or [])[:3]) or "—"
        pnl_s  = f"+${pnl:,.0f}" if pnl >= 0 else f"-${abs(pnl):,.0f}"
        msg   += f"{i+1}. `{short}` | {pnl_s} | _{tags}_\n"

    msg += f"\n🔗 [Lihat di GMGN](https://gmgn.ai/sol/token/{addr})"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    now = time.time()
    active_seen = sum(1 for t in seen_addresses.values() if (now - t) / 3600 <= SEEN_TTL_HOURS)
    await update.message.reply_text(
        f"✅ *ONLINE* | v7\n━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Scan interval: {config.CHECK_INTERVAL_MINUTES} menit\n"
        f"🔁 Seen aktif: {active_seen} (TTL {SEEN_TTL_HOURS}j)\n"
        f"📋 Hold watchlist: {len(hold_watchlist)} koin\n"
        f"🧠 SM wallets manual: {len(sm_wallets)}\n"
        f"🔍 GMGN auto SM: {'✅' if config.ENABLE_GMGN_SMART_MONEY else '❌'}\n"
        f"📡 Helius API: {'✅' if config.HELIUS_API_KEY else '⚠️ belum diset'}\n"
        f"💾 State: {'✅' if os.path.exists(STATE_FILE) else '❌'}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"🌙≥{config.SCORE_MOONBAG} | 🎯≥{config.SCORE_SWING} | ⚡≥{config.SCORE_SCALP}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await update.message.reply_text(
        f"🔍 *Filter Aktif v7*\n━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Min Liq: ${config.MIN_LIQUIDITY_USD:,}\n"
        f"📊 Min Vol 1h: ${config.MIN_VOLUME_1H:,}\n"
        f"🚫 Block keywords: ✅ DIHAPUS (semua nama boleh)\n"
        f"⏰ Age limit: ✅ DIHAPUS (diganti age scoring)\n"
        f"  • <6 jam: +0 penalty\n"
        f"  • 6-24 jam: -5\n"
        f"  • 1-7 hari: -10\n"
        f"  • 7-30 hari: -20\n"
        f"  • 30+ hari: -35 (perlu resurrection)\n"
        f"🛡️ Anti wash trading: ON\n"
        f"📉 Anti distribusi: ON\n"
        f"📉 Anti downtrend: ON\n"
        f"⏳ Dedup TTL: {SEEN_TTL_HOURS} jam\n"
        f"🔍 GMGN SM auto-check: {'ON' if config.ENABLE_GMGN_SMART_MONEY else 'OFF'}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    n = len(seen_addresses)
    seen_addresses.clear()
    save_state()
    await update.message.reply_text(f"✅ Cache reset ({n} entri). Hold & SM wallets aman.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ══════════════════════════════════════════════════════════════
#  BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════

async def background_scanner(bot):
    while True:
        await asyncio.sleep(config.CHECK_INTERVAL_MINUTES * 60)
        await do_scan(bot, manual=False)

async def background_wallet_poller(bot):
    await asyncio.sleep(60)
    while True:
        if sm_wallets and config.HELIUS_API_KEY:
            await poll_smart_money_wallets(bot)
        await asyncio.sleep(config.WALLET_POLL_INTERVAL_MIN * 60)


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def main():
    print("=" * 50)
    print("  MEMECOIN MONITOR v7")
    print("=" * 50)

    load_state()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for cmd, handler in [
        ("start",      cmd_start),
        ("scan",       cmd_scan),
        ("hold",       cmd_hold),
        ("wallet",     cmd_wallet),
        ("gmgn",       cmd_gmgn),
        ("status",     cmd_status),
        ("filter",     cmd_filter),
        ("clearcache", cmd_clearcache),
        ("help",       cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    async with app:
        await app.start()
        bot = app.bot

        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                "🤖 *MEMECOIN MONITOR v7 AKTIF*\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "✅ GMGN auto smart money (tanpa API key)\n"
                "✅ Age scoring — tidak ada hard limit umur\n"
                "✅ Block keywords dihapus\n"
                "✅ Dedup pair duplikat (ambil likuiditas tertinggi)\n"
                "✅ Contract address tampil di setiap alert\n"
                "✅ Multi-pair URL di alert\n"
                "✅ /gmgn <address> — cek insider manual\n"
                "✅ Hold + SM wallet monitoring\n"
                "✅ Persistent state (restart-safe)\n\n"
                "/scan — scan manual\n"
                "/gmgn <address> — cek smart money token\n"
                "/hold <address> — pantau koin hold\n"
                "⏳ Auto scan pertama dimulai..."
            ),
            parse_mode=ParseMode.MARKDOWN
        )

        await do_scan(bot, manual=False)
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.gather(
            background_scanner(bot),
            background_wallet_poller(bot),
        )


if __name__ == "__main__":
    asyncio.run(main())
