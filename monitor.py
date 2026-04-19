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

# ─── STATE ───────────────────────────────────────────────────
volume_history = {}       # address -> [(timestamp, vol_1h)]
seen_addresses = {}       # address -> timestamp (kapan pertama di-alert) — TTL based
hold_watchlist = set()    # address yang sedang di-hold user

SEEN_TTL_HOURS = 6        # koin tidak akan di-alert ulang dalam 6 jam
STATE_FILE = "state.json"


# ─── PERSISTENT STATE ────────────────────────────────────────

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "seen_addresses": seen_addresses,
                "volume_history": {
                    k: [[t, v] for t, v in vals]
                    for k, vals in volume_history.items()
                },
                "hold_watchlist": list(hold_watchlist)
            }, f)
        print("  State saved.")
    except Exception as e:
        print(f"  Save state error: {e}")


def load_state():
    global seen_addresses, volume_history, hold_watchlist
    if not os.path.exists(STATE_FILE):
        print("  No state file found, starting fresh.")
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        seen_addresses = data.get("seen_addresses", {})
        volume_history = {
            k: [(t, v) for t, v in vals]
            for k, vals in data.get("volume_history", {}).items()
        }
        hold_watchlist = set(data.get("hold_watchlist", []))
        print(f"  State loaded: {len(seen_addresses)} seen, {len(hold_watchlist)} watched, {len(volume_history)} vol history")
    except Exception as e:
        print(f"  Load state error: {e}")


# ─── TTL-BASED DEDUP ─────────────────────────────────────────

def is_seen(address):
    if address not in seen_addresses:
        return False
    elapsed = (time.time() - seen_addresses[address]) / 3600
    if elapsed > SEEN_TTL_HOURS:
        del seen_addresses[address]   # expired, boleh muncul lagi
        return False
    return True


def mark_seen(address):
    seen_addresses[address] = time.time()


def cleanup_seen():
    """Buang hanya yang sudah expired, bukan clear semua."""
    now = time.time()
    expired = [k for k, t in seen_addresses.items()
               if (now - t) / 3600 > SEEN_TTL_HOURS]
    for k in expired:
        del seen_addresses[k]
    if expired:
        print(f"  Cleaned {len(expired)} expired seen_addresses")


# ─── VOLUME TRACKING ─────────────────────────────────────────

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
    prev = [v for _, v in history[:-1]]
    avg_prev = sum(prev) / len(prev)
    if avg_prev < 200 and current_vol_1h > 5000:
        return True, round(current_vol_1h / max(avg_prev, 1))
    return False, 0


# ─── NITTER SCRAPER ──────────────────────────────────────────

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.lucahammer.com",
]

def search_twitter_mentions(symbol, address):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    tweets = []

    queries = [f"${symbol}", address[:20]]

    for query in queries:
        for instance in NITTER_INSTANCES:
            try:
                url = f"{instance}/search?q={requests.utils.quote(query)}&f=tweets"
                r = requests.get(url, headers=headers, timeout=6)
                if r.status_code != 200:
                    continue
                pattern = r'class="tweet-content[^"]*"[^>]*>(.*?)</div>'
                matches = re.findall(pattern, r.text, re.DOTALL)
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

    pos_words = ["moon", "gem", "100x", "buy", "bullish", "fire", "🔥", "🚀",
                 "💎", "ape", "alpha", "early", "send", "go", "launch"]
    neg_words = ["rug", "scam", "dump", "rekt", "dead", "honeypot",
                 "avoid", "warning", "careful", "sus", "fake"]

    pos = sum(1 for t in tweets for w in pos_words if w in t.lower())
    neg = sum(1 for t in tweets for w in neg_words if w in t.lower())

    bonus = min(len(tweets) * 2, 10)
    sentiment = "🟢 Positif" if pos > neg else "🔴 Negatif" if neg > pos else "🟡 Netral"

    if neg > pos:
        bonus -= 10

    return bonus, f"📱 Twitter: {len(tweets)} mention | {sentiment}"


# ─── DATA FETCHING ───────────────────────────────────────────

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
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                timeout=8
            )
            if r.status_code == 200:
                pairs = r.json().get("pairs", [])
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

    # EP3: Direct search pump sol (fresh graduates)
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

    print(f"  Total: {len(all_pairs)}")
    return all_pairs


def get_token_details(pair):
    try:
        base = pair.get("baseToken", {})
        liq = pair.get("liquidity", {})
        vol = pair.get("volume", {})
        pc = pair.get("priceChange", {})
        txns = pair.get("txns", {})

        created_at = pair.get("pairCreatedAt", 0)
        age_h = (time.time() - created_at / 1000) / 3600 if created_at else 999

        h1 = txns.get("h1", {})
        h6 = txns.get("h6", {})
        h24 = txns.get("h24", {})

        h1_buys  = h1.get("buys", 0)
        h1_sells = h1.get("sells", 0)
        h6_buys  = h6.get("buys", 0)
        h6_sells = h6.get("sells", 0)
        h24_buys  = h24.get("buys", 0)
        h24_sells = h24.get("sells", 0)

        makers = (h24.get("makers") or h6.get("makers") or
                  h1.get("makers") or pair.get("makers") or 0)

        v1h  = float(vol.get("h1", 0) or 0)
        v6h  = float(vol.get("h6", 0) or 0)
        v24h = float(vol.get("h24", 0) or 0)

        avg_6h_per_hour = v6h / 6 if v6h > 0 else 0
        vol_accel = round(v1h / avg_6h_per_hour, 2) if avg_6h_per_hour > 50 else 0

        wash = 0
        abpm = 0
        if makers > 0 and h24_buys > 0:
            abpm = h24_buys / makers
            if abpm > 6:     wash = 3
            elif abpm > 4:   wash = 2
            elif abpm > 2.5: wash = 1

        pc_1h = float(pc.get("h1", 0) or 0)
        pc_6h = float(pc.get("h6", 0) or 0)

        return {
            "name":          base.get("name", "Unknown"),
            "symbol":        base.get("symbol", "???"),
            "address":       base.get("address", ""),
            "mcap":          float(pair.get("marketCap", 0) or 0),
            "liquidity_usd": float(liq.get("usd", 0) or 0),
            "v1h": v1h, "v6h": v6h, "v24h": v24h,
            "vol_accel":     vol_accel,
            "pc_1h": pc_1h, "pc_6h": pc_6h,
            "pc_24h":        float(pc.get("h24", 0) or 0),
            "pc_5m":         float(pc.get("m5", 0) or 0),
            "age_h":         round(age_h, 1),
            "h1_bsr":  round(h1_buys  / max(h1_sells,  1), 2),
            "h6_bsr":  round(h6_buys  / max(h6_sells,  1), 2),
            "h24_bsr": round(h24_buys / max(h24_sells, 1), 2),
            "makers":        makers,
            "wash":          wash,
            "abpm":          round(abpm, 1),
            "pair_url":      pair.get("url", ""),
        }
    except Exception as e:
        print(f"  Parse err: {e}")
        return None


# ─── HARD FILTER ─────────────────────────────────────────────

BLOCK_NAMES = [
    "asteroid", "pepe2", "shib2", "inu2", "classic", "fake",
    "copy", "v2 ", " v3", "2.0", "reborn", "remix", "clone",
    "generational", "rekt",
]

def passes_filter(token):
    if token["mcap"] <= 0:
        return False, "MCap invalid"
    if token["liquidity_usd"] < config.MIN_LIQUIDITY_USD:
        return False, "Liquidity rendah"
    if token["age_h"] > config.MAX_AGE_HOURS:
        return False, "Terlalu tua"
    if token["wash"] >= 3:
        return False, f"Wash trading ({token['abpm']}x/wallet)"
    if token["v1h"] < 1000:
        return False, "Vol 1h < $1k"
    if token["pc_1h"] < -5 and token["vol_accel"] < 1.0:
        return False, f"Distribusi: price -{abs(token['pc_1h'])}% + vol accel {token['vol_accel']}x"
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


# ─── SCORING ─────────────────────────────────────────────────

def score_token(token):
    score = 0
    reasons = []
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
        score += 20
        reasons.append(f"🆕 Fresh graduate ({age} jam)")
        if token["h1_bsr"] > 3:
            score += 30; reasons.append(f"💚 Buy pressure kuat ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 2:
            score += 20; reasons.append(f"✅ Buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.3:
            score += 10; reasons.append(f"🟡 Buy moderate ({token['h1_bsr']}x)")
        if token["v1h"] > 50000:
            score += 20; reasons.append(f"🔥 Vol 1h: ${token['v1h']:,.0f}")
        elif token["v1h"] > 10000:
            score += 12; reasons.append(f"📈 Vol 1h: ${token['v1h']:,.0f}")
        elif token["v1h"] > 3000:
            score += 5
        if token["mcap"] < 100000:
            score += 15; reasons.append(f"💰 MCap kecil (${token['mcap']:,.0f})")
        elif token["mcap"] < 500000:
            score += 8; reasons.append(f"💰 MCap (${token['mcap']:,.0f})")

    elif ttype == "RESURRECTION":
        score += 30
        reasons.append(f"⚡ RESURRECTION! Volume {res_mult}x dari baseline")
        if token["h1_bsr"] > 2:
            score += 25; reasons.append(f"💚 Buy pressure post-res ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.3:
            score += 15; reasons.append(f"🟡 Buy ada ({token['h1_bsr']}x)")
        if token["pc_1h"] > 50:
            score += 20; reasons.append(f"🚀 +{token['pc_1h']}% dalam 1 jam!")
        elif token["pc_1h"] > 20:
            score += 10; reasons.append(f"📈 +{token['pc_1h']}%")

    elif ttype == "MOMENTUM":
        score += 10; reasons.append(f"🚀 Momentum +{token['pc_1h']}%")
        if token["h1_bsr"] > 3:
            score += 25; reasons.append(f"💚 Buy pressure ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 2:
            score += 18; reasons.append(f"✅ Buy dominan ({token['h1_bsr']}x)")
        if token["vol_accel"] > 4:
            score += 20; reasons.append(f"🔥 Vol accel {token['vol_accel']}x")
        elif token["vol_accel"] > 2:
            score += 12; reasons.append(f"📊 Vol accel {token['vol_accel']}x")

    else:  # ACCUMULATION / NORMAL
        if token["vol_accel"] > 3:
            score += 20; reasons.append(f"📊 Vol accel {token['vol_accel']}x")
        elif token["vol_accel"] > 2:
            score += 12; reasons.append(f"📊 Vol accel {token['vol_accel']}x")
        if token["h1_bsr"] > 2.5:
            score += 20; reasons.append(f"💚 Buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] > 1.5:
            score += 12; reasons.append(f"🟡 Sedikit buy dominan ({token['h1_bsr']}x)")
        elif token["h1_bsr"] < 0.7:
            score -= 20; warnings.append("🔴 Sell pressure tinggi!")

    # Universal
    if token["mcap"] > 0:
        lr = token["liquidity_usd"] / token["mcap"]
        if lr > 0.15:   score += 10; reasons.append(f"💧 Liquidity sehat ({round(lr*100,1)}%)")
        elif lr > 0.05: score += 5
        elif lr < 0.02: score -= 10; warnings.append("⚠️ Liquidity tipis!")

    if token["wash"] == 2:
        score -= 15; warnings.append(f"🔶 Wash trading suspicious ({token['abpm']}x/wallet)")
    elif token["wash"] == 1:
        score -= 5; warnings.append(f"⚠️ Sedikit suspicious ({token['abpm']}x/wallet)")

    if token["pc_1h"] < -25:
        score -= 20; warnings.append(f"📉 Dump -{abs(token['pc_1h'])}% 1h!")

    if token["pc_5m"] > 5:
        score += 5; reasons.append(f"🟢 +{token['pc_5m']}% dalam 5 menit terakhir")

    return score, reasons, warnings, ttype


def get_signal(score, ttype):
    if score >= config.SCORE_MOONBAG:
        desc = "MCap kecil + momentum awal = potensi 100x+" if ttype in ["FRESH_GRADUATE","RESURRECTION"] else "Hold berminggu-minggu"
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


# ─── TELEGRAM ALERTS ─────────────────────────────────────────

async def send_alert(bot, token, score, sig, desc, reasons, warnings, ttype, tw=None):
    wash_l = ["✅ Organik","🟡 Suspicious","🔶 High Suspicious","🔴 Wash"][min(token["wash"],3)]
    ttype_e = {"FRESH_GRADUATE":"🆕","RESURRECTION":"⚡","MOMENTUM":"🚀","ACCUMULATION":"📈"}.get(ttype,"📊")

    msg = (
        f"{sig}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token['name']}* (${token['symbol']})\n"
        f"{ttype_e} Type: *{ttype}*\n"
        f"📊 Score: *{score}/100*\n"
        f"💡 {desc}\n\n"
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


# ─── HOLD WATCHLIST ──────────────────────────────────────────

def fetch_pair_by_address(address):
    """Ambil data terbaru satu koin dari DexScreener."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=8
        )
        if r.status_code != 200:
            return None
        pairs = r.json().get("pairs", [])
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None
        return max(sol_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
    except:
        return None


async def check_hold_watchlist(bot):
    """Dipanggil setiap scan — re-evaluate semua koin yang sedang di-hold."""
    if not hold_watchlist:
        return

    print(f"  Checking {len(hold_watchlist)} hold positions...")
    for address in list(hold_watchlist):
        try:
            pair = fetch_pair_by_address(address)
            if not pair:
                print(f"  Hold check: no data for {address[:10]}")
                continue

            t = get_token_details(pair)
            if not t:
                continue

            track_volume(t["address"], t["v1h"])
            score, reasons, warnings, ttype = score_token(t)

            if score >= 60:
                status_emoji = "🟢"
                status_text  = "Kondisi BAGUS — hold lanjut"
            elif score >= 40:
                status_emoji = "🟡"
                status_text  = "Kondisi MODERAT — pantau"
            else:
                status_emoji = "🔴"
                status_text  = "Kondisi MELEMAH — pertimbangkan exit!"

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
            hold_msg += f"\n\n_Hapus dari watchlist: /hold remove {address}_"

            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=hold_msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            await asyncio.sleep(1)

        except Exception as e:
            print(f"  Hold check error {address[:8]}: {e}")


# ─── SCAN ────────────────────────────────────────────────────

async def do_scan(bot, manual=False):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{ts}] {'Manual' if manual else 'Auto'} scan")

    if manual:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=f"🔄 *Manual Scan Dimulai* ({ts} WIB)\n⏳ Sedang memindai...",
            parse_mode=ParseMode.MARKDOWN
        )

    pairs = get_solana_pairs()
    sent = 0
    filtered = 0

    for pair in pairs:
        t = get_token_details(pair)
        if not t or not t["address"]:
            continue

        track_volume(t["address"], t["v1h"])

        # ✅ FIX: Gunakan TTL-based dedup (bukan set biasa)
        if is_seen(t["address"]):
            continue

        ok, reason = passes_filter(t)
        if not ok:
            print(f"  ✗ {t['name'][:18]:<18} {reason}")
            filtered += 1
            continue

        score, reasons, warnings, ttype = score_token(t)
        sig, desc = get_signal(score, ttype)

        print(f"  {ttype[:4]} | {t['name'][:15]:<15} | S:{score:>3} | MC:${t['mcap']:>10,.0f} | V1h:${t['v1h']:>8,.0f} | BSR:{t['h1_bsr']}")

        if sig:
            mark_seen(t["address"])  # ✅ FIX: mark dengan timestamp

            tw_bonus, tw_summary = 0, None
            if score >= 55 and config.ENABLE_TWITTER_CHECK:
                tw_bonus, tw_summary = analyze_twitter(t["name"], t["symbol"], t["address"])
                score += tw_bonus

            print(f"  >>> {sig} (score={score})")
            await send_alert(bot, t, score, sig, desc, reasons, warnings, ttype, tw_summary)
            sent += 1
            await asyncio.sleep(1.5)

    # ✅ FIX: Hold watchlist check setiap scan
    await check_hold_watchlist(bot)

    if manual:
        hold_note = f"\n📋 Hold watchlist: {len(hold_watchlist)} koin dipantau" if hold_watchlist else ""
        note = "\n\n💤 Belum ada koin memenuhi kriteria." if sent == 0 else ""
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"✅ *Scan Selesai*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Dipindai: {len(pairs)}\n"
                f"🚫 Difilter: {filtered}\n"
                f"🔔 Alert: {sent}\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB{note}{hold_note}"
            ),
            parse_mode=ParseMode.MARKDOWN
        )

    # ✅ FIX: Cleanup gentle (hanya expired), bukan clear total
    cleanup_seen()

    if len(volume_history) > 2000:
        oldest = sorted(volume_history, key=lambda k: volume_history[k][-1][0])[:500]
        for k in oldest:
            del volume_history[k]

    # ✅ FIX: Simpan state ke disk setelah setiap scan
    save_state()

    print(f"  Done: {sent} alerts, {filtered} filtered")


# ─── COMMANDS ────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v5*\n━━━━━━━━━━━━━━━━━━━\n"
        "/scan — Scan manual\n"
        "/hold <address> — Pantau koin yang di-hold\n"
        "/hold list — Lihat semua koin yang dipantau\n"
        "/hold remove <address> — Hapus dari pantauan\n"
        "/status — Status bot\n"
        "/filter — Filter aktif\n"
        "/clearcache — Reset cache seen\n"
        "/help — Bantuan",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await do_scan(context.bot, manual=True)

async def cmd_hold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /hold <address>        — tambah koin ke watchlist
    /hold list             — lihat semua koin yang dipantau
    /hold remove <address> — hapus dari watchlist
    /hold check            — cek kondisi semua hold sekarang
    """
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return

    args = context.args
    if not args:
        count = len(hold_watchlist)
        await update.message.reply_text(
            f"📋 *Hold Watchlist* ({count} koin)\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Perintah:\n"
            "/hold <address> — tambah\n"
            "/hold list — lihat semua\n"
            "/hold remove <address> — hapus\n"
            "/hold check — cek kondisi sekarang",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if args[0] == "list":
        if not hold_watchlist:
            await update.message.reply_text("📋 Watchlist kosong.\n\nGunakan /hold <address> untuk menambahkan koin.")
        else:
            lines = "\n".join(f"• `{a}`" for a in hold_watchlist)
            await update.message.reply_text(
                f"📋 *Hold Watchlist ({len(hold_watchlist)} koin):*\n{lines}",
                parse_mode=ParseMode.MARKDOWN
            )
        return

    if args[0] == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /hold remove <address>")
            return
        addr = args[1].strip()
        if addr in hold_watchlist:
            hold_watchlist.discard(addr)
            save_state()
            await update.message.reply_text(f"✅ Dihapus dari watchlist:\n`{addr}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Address tidak ada di watchlist.")
        return

    if args[0] == "check":
        await update.message.reply_text("🔄 Mengecek kondisi hold positions...")
        await check_hold_watchlist(context.bot)
        return

    # Default: tambah address
    addr = args[0].strip()
    if len(addr) < 20:
        await update.message.reply_text("❌ Address tidak valid (terlalu pendek).")
        return
    if addr in hold_watchlist:
        await update.message.reply_text(f"⚠️ Address ini sudah ada di watchlist.")
        return

    # Validasi: cek apakah address valid di DexScreener
    await update.message.reply_text("⏳ Memvalidasi address...")
    pair = fetch_pair_by_address(addr)
    if not pair:
        await update.message.reply_text(
            "❌ Address tidak ditemukan di DexScreener.\n"
            "Pastikan address token Solana yang benar."
        )
        return

    t = get_token_details(pair)
    if not t:
        await update.message.reply_text("❌ Gagal mengambil data token.")
        return

    hold_watchlist.add(addr)
    save_state()
    await update.message.reply_text(
        f"✅ *Ditambahkan ke Hold Watchlist!*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 {t['name']} (${t['symbol']})\n"
        f"💰 MCap: ${t['mcap']:,.0f}\n"
        f"💧 Liq: ${t['liquidity_usd']:,.0f}\n\n"
        f"Bot akan update kondisi koin ini setiap {config.CHECK_INTERVAL_MINUTES} menit.\n"
        f"Hapus: /hold remove {addr}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    tw = "✅ ON" if config.ENABLE_TWITTER_CHECK else "❌ OFF"

    # Hitung berapa seen yang masih aktif (belum expired)
    now = time.time()
    active_seen = sum(1 for t in seen_addresses.values() if (now - t) / 3600 <= SEEN_TTL_HOURS)

    await update.message.reply_text(
        f"✅ *ONLINE* | v5\n━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Scan interval: {config.CHECK_INTERVAL_MINUTES} menit\n"
        f"📝 Seen (aktif): {active_seen} koin (TTL {SEEN_TTL_HOURS}j)\n"
        f"📋 Hold watchlist: {len(hold_watchlist)} koin\n"
        f"📈 Vol history: {len(volume_history)} token\n"
        f"📱 Twitter: {tw}\n"
        f"💾 State: {'✅ Tersimpan' if os.path.exists(STATE_FILE) else '❌ Belum ada'}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"🌙≥{config.SCORE_MOONBAG} | 🎯≥{config.SCORE_SWING} | ⚡≥{config.SCORE_SCALP}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await update.message.reply_text(
        f"🔍 *Filter Aktif v5*\n━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Min Liq: ${config.MIN_LIQUIDITY_USD:,}\n"
        f"📊 Min Vol 1h: $1,000\n"
        f"⏰ Max Age: {config.MAX_AGE_HOURS} jam\n"
        f"🛡️ Anti wash trading: ON\n"
        f"🚫 Anti copycat/rekt names: ON\n"
        f"📉 Anti distribusi: ON\n"
        f"📉 Anti downtrend 6h: ON\n"
        f"⏳ Dedup TTL: {SEEN_TTL_HOURS} jam",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    old_count = len(seen_addresses)
    seen_addresses.clear()
    save_state()
    await update.message.reply_text(
        f"✅ Cache di-reset ({old_count} entri dihapus).\n"
        f"Hold watchlist ({len(hold_watchlist)} koin) tetap aman.\n"
        f"Scan berikutnya akan fresh."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ─── BACKGROUND ──────────────────────────────────────────────

async def background_scanner(bot):
    while True:
        await asyncio.sleep(config.CHECK_INTERVAL_MINUTES * 60)
        await do_scan(bot, manual=False)


# ─── MAIN ────────────────────────────────────────────────────

async def main():
    print("=" * 50)
    print("  MEMECOIN MONITOR v5")
    print("=" * 50)

    # ✅ FIX: Load persistent state saat startup
    load_state()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for cmd, handler in [
        ("start",      cmd_start),
        ("scan",       cmd_scan),
        ("hold",       cmd_hold),
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
                "🤖 *MEMECOIN MONITOR v5 AKTIF*\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "✅ Anti-distribusi filter: ON\n"
                "✅ Fresh Graduate detection: ON\n"
                "✅ Volume Resurrection: ON\n"
                "✅ Twitter mention check: ON\n"
                "✅ TTL-based dedup (6 jam): ON\n"
                "✅ Persistent state (restart-safe): ON\n"
                "✅ Hold Watchlist monitoring: ON\n\n"
                "/scan — scan manual sekarang\n"
                "/hold <address> — pantau koin yang di-hold\n"
                "⏳ Auto scan pertama dimulai..."
            ),
            parse_mode=ParseMode.MARKDOWN
        )

        await do_scan(bot, manual=False)
        await app.updater.start_polling(drop_pending_updates=True)
        await background_scanner(bot)


if __name__ == "__main__":
    asyncio.run(main())
