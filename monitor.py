import requests
import time
import asyncio
import re
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import config

# ─── STATE ───────────────────────────────────────────────────
volume_history = {}       # address -> [(timestamp, vol_1h)]
seen_addresses = set()    # dedup by address saja, bukan address+mcap


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

        # Makers — coba semua posisi yang mungkin
        makers = (h24.get("makers") or h6.get("makers") or
                  h1.get("makers") or pair.get("makers") or 0)

        v1h  = float(vol.get("h1", 0) or 0)
        v6h  = float(vol.get("h6", 0) or 0)
        v24h = float(vol.get("h24", 0) or 0)

        # Vol acceleration: v1h vs rata-rata per jam dari v6h
        avg_6h_per_hour = v6h / 6 if v6h > 0 else 0
        vol_accel = round(v1h / avg_6h_per_hour, 2) if avg_6h_per_hour > 50 else 0

        # Wash trading
        wash = 0
        abpm = 0
        if makers > 0 and h24_buys > 0:
            abpm = h24_buys / makers
            if abpm > 6:   wash = 3
            elif abpm > 4: wash = 2
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
    "generational", "rekt",   # block GREKT secara eksplisit
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

    # === KRITERIA BARU: BUKAN DISTRIBUSI ===
    # Jika harga TURUN + volume decelerating = orang keluar, bukan masuk
    if token["pc_1h"] < -5 and token["vol_accel"] < 1.0:
        return False, f"Distribusi: price -{abs(token['pc_1h'])}% + vol accel {token['vol_accel']}x"

    # Jika price turun terus 6 jam terakhir juga
    if token["pc_1h"] < -10 and token["pc_6h"] < -15:
        return False, f"Downtrend: 1h={token['pc_1h']}% 6h={token['pc_6h']}%"

    # Copycat / nama suspicious
    name_l = (token["name"] + " " + token["symbol"]).lower()
    for kw in BLOCK_NAMES:
        if kw in name_l:
            return False, f"Block keyword: {kw}"

    # Volume manipulasi
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

    # Tentukan tipe
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

    # === SCORE BY TYPE ===

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

    # === UNIVERSAL ===
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

    # Bonus: 5m candle hijau = momentum fresh
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


# ─── TELEGRAM ────────────────────────────────────────────────

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

        # Dedup by address
        if t["address"] in seen_addresses:
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
            seen_addresses.add(t["address"])

            tw_bonus, tw_summary = 0, None
            if score >= 55 and config.ENABLE_TWITTER_CHECK:
                tw_bonus, tw_summary = analyze_twitter(t["name"], t["symbol"], t["address"])
                score += tw_bonus

            print(f"  >>> {sig} (score={score})")
            await send_alert(bot, t, score, sig, desc, reasons, warnings, ttype, tw_summary)
            sent += 1
            await asyncio.sleep(1.5)

    if manual:
        note = "\n\n💤 Belum ada koin memenuhi kriteria." if sent == 0 else ""
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"✅ *Scan Selesai*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Dipindai: {len(pairs)}\n"
                f"🚫 Difilter: {filtered}\n"
                f"🔔 Alert: {sent}\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB{note}"
            ),
            parse_mode=ParseMode.MARKDOWN
        )

    if len(seen_addresses) > 2000:
        seen_addresses.clear()
    if len(volume_history) > 2000:
        oldest = sorted(volume_history, key=lambda k: volume_history[k][-1][0])[:500]
        for k in oldest:
            del volume_history[k]

    print(f"  Done: {sent} alerts, {filtered} filtered")


# ─── COMMANDS ────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v4*\n━━━━━━━━━━━━━━━━━━━\n"
        "/scan — Scan manual\n/status — Status bot\n"
        "/filter — Filter aktif\n/clearcache — Reset cache\n/help — Bantuan",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await do_scan(context.bot, manual=True)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    tw = "✅ ON" if config.ENABLE_TWITTER_CHECK else "❌ OFF"
    await update.message.reply_text(
        f"✅ *ONLINE* | v4\n━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Scan interval: {config.CHECK_INTERVAL_MINUTES} menit\n"
        f"📝 Seen addresses: {len(seen_addresses)}\n"
        f"📈 Vol history: {len(volume_history)} token\n"
        f"📱 Twitter: {tw}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"🌙≥{config.SCORE_MOONBAG} | 🎯≥{config.SCORE_SWING} | ⚡≥{config.SCORE_SCALP}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await update.message.reply_text(
        f"🔍 *Filter Aktif v4*\n━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Min Liq: ${config.MIN_LIQUIDITY_USD:,}\n"
        f"📊 Min Vol 1h: $1,000\n"
        f"⏰ Max Age: {config.MAX_AGE_HOURS} jam\n"
        f"🛡️ Anti wash trading: ON\n"
        f"🚫 Anti copycat/rekt names: ON\n"
        f"📉 Anti distribusi (dump+vol turun): ON\n"
        f"📉 Anti downtrend 6h: ON",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    seen_addresses.clear()
    await update.message.reply_text("✅ Cache di-reset. Scan berikutnya akan fresh.")

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
    print("  MEMECOIN MONITOR v4")
    print("=" * 50)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for cmd, handler in [
        ("start", cmd_start), ("scan", cmd_scan), ("status", cmd_status),
        ("filter", cmd_filter), ("clearcache", cmd_clearcache), ("help", cmd_help)
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    async with app:
        await app.start()
        bot = app.bot

        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                "🤖 *MEMECOIN MONITOR v4 AKTIF*\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "✅ Anti-distribusi filter: ON\n"
                "✅ Fresh Graduate detection: ON\n"
                "✅ Volume Resurrection: ON\n"
                "✅ Twitter mention check: ON\n"
                "✅ Address-based dedup: ON\n\n"
                "/scan — scan manual sekarang\n"
                "⏳ Auto scan pertama dimulai..."
            ),
            parse_mode=ParseMode.MARKDOWN
        )

        await do_scan(bot, manual=False)
        await app.updater.start_polling(drop_pending_updates=True)
        await background_scanner(bot)


if __name__ == "__main__":
    asyncio.run(main())
