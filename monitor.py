import requests
import time
import asyncio
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import config

# ─── DATA FETCHING ───────────────────────────────────────────

def get_solana_pairs():
    """Ambil token Solana dari DexScreener"""
    all_pairs = []

    try:
        url1 = "https://api.dexscreener.com/token-profiles/latest/v1"
        r1 = requests.get(url1, timeout=10)
        if r1.status_code == 200:
            data1 = r1.json()
            sol_tokens = [t for t in data1 if t.get("chainId") == "solana"]
            print(f"  Token profiles found: {len(sol_tokens)}")
            for token in sol_tokens[:30]:
                addr = token.get("tokenAddress", "")
                if not addr:
                    continue
                try:
                    r_pair = requests.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                        timeout=8
                    )
                    if r_pair.status_code == 200:
                        pairs = r_pair.json().get("pairs", [])
                        if pairs:
                            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                            all_pairs.append(best)
                    time.sleep(0.2)
                except:
                    continue
    except Exception as e:
        print(f"Error endpoint 1: {e}")

    try:
        url2 = "https://api.dexscreener.com/token-boosts/latest/v1"
        r2 = requests.get(url2, timeout=10)
        if r2.status_code == 200:
            data2 = r2.json()
            sol_boosted = [t for t in data2 if t.get("chainId") == "solana"]
            print(f"  Boosted tokens found: {len(sol_boosted)}")
            for token in sol_boosted[:20]:
                addr = token.get("tokenAddress", "")
                if not addr:
                    continue
                try:
                    r_pair = requests.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                        timeout=8
                    )
                    if r_pair.status_code == 200:
                        pairs = r_pair.json().get("pairs", [])
                        if pairs:
                            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                            existing = [p.get("baseToken", {}).get("address") for p in all_pairs]
                            if best.get("baseToken", {}).get("address") not in existing:
                                all_pairs.append(best)
                    time.sleep(0.2)
                except:
                    continue
    except Exception as e:
        print(f"Error endpoint 2: {e}")

    try:
        url3 = "https://api.dexscreener.com/latest/dex/search?q=solana"
        r3 = requests.get(url3, timeout=10)
        if r3.status_code == 200:
            data3 = r3.json()
            sol_pairs = [p for p in data3.get("pairs", []) if p.get("chainId") == "solana"]
            print(f"  Direct pairs found: {len(sol_pairs)}")
            for p in sol_pairs[:20]:
                existing = [x.get("baseToken", {}).get("address") for x in all_pairs]
                if p.get("baseToken", {}).get("address") not in existing:
                    all_pairs.append(p)
    except Exception as e:
        print(f"Error endpoint 3: {e}")

    print(f"Total pairs collected: {len(all_pairs)}")
    return all_pairs


def get_token_details(pair):
    try:
        base = pair.get("baseToken", {})
        liquidity = pair.get("liquidity", {})
        volume = pair.get("volume", {})
        price_change = pair.get("priceChange", {})
        txns = pair.get("txns", {})

        created_at = pair.get("pairCreatedAt", 0)
        age_hours = (time.time() - created_at / 1000) / 3600 if created_at else 0

        h24_buys = txns.get("h24", {}).get("buys", 0)
        h24_sells = txns.get("h24", {}).get("sells", 0)
        h1_buys = txns.get("h1", {}).get("buys", 0)
        h1_sells = txns.get("h1", {}).get("sells", 0)

        makers_24h = pair.get("makers", 0) or 0

        # Wash trading detection
        wash_score = 0
        avg_buys_per_maker = 0
        if h24_buys > 0 and makers_24h > 0:
            avg_buys_per_maker = h24_buys / makers_24h
            if avg_buys_per_maker > 5:
                wash_score = 3
            elif avg_buys_per_maker > 3:
                wash_score = 2
            elif avg_buys_per_maker > 2:
                wash_score = 1

        return {
            "name": base.get("name", "Unknown"),
            "symbol": base.get("symbol", "???"),
            "address": base.get("address", ""),
            "price_usd": float(pair.get("priceUsd", 0) or 0),
            "mcap": float(pair.get("marketCap", 0) or 0),
            "liquidity_usd": float(liquidity.get("usd", 0) or 0),
            "volume_24h": float(volume.get("h24", 0) or 0),
            "volume_1h": float(volume.get("h1", 0) or 0),
            "price_change_5m": float(price_change.get("m5", 0) or 0),
            "price_change_1h": float(price_change.get("h1", 0) or 0),
            "price_change_24h": float(price_change.get("h24", 0) or 0),
            "age_hours": round(age_hours, 1),
            "buy_sell_ratio": round(h24_buys / max(h24_sells, 1), 2),
            "h1_buy_sell_ratio": round(h1_buys / max(h1_sells, 1), 2),
            "h24_buys": h24_buys,
            "h24_sells": h24_sells,
            "makers_24h": makers_24h,
            "wash_score": wash_score,
            "avg_buys_per_maker": round(avg_buys_per_maker, 1),
            "pair_url": pair.get("url", ""),
            "dex_id": pair.get("dexId", ""),
        }
    except Exception as e:
        print(f"Error parsing pair: {e}")
        return None


# ─── FILTER ENGINE ───────────────────────────────────────────

COPYCAT_KEYWORDS = [
    "asteroid", "pepe2", "shib2", "inu2", "elon2", "doge2",
    "classic", "original", "real", "fake", "copy", "v2", "v3",
    "2.0", "reborn", "remix", "clone"
]

def passes_filter(token):
    if token["liquidity_usd"] < config.MIN_LIQUIDITY_USD:
        return False, "Liquidity terlalu rendah"
    if token["volume_24h"] < config.MIN_VOLUME_24H:
        return False, "Volume 24h terlalu rendah"
    if token["age_hours"] < config.MIN_AGE_HOURS:
        return False, "Terlalu baru"
    if token["age_hours"] > config.MAX_AGE_HOURS:
        return False, "Terlalu tua"
    if token["mcap"] <= 0:
        return False, "Market cap tidak valid"
    if token["wash_score"] >= 3:
        return False, f"Wash trading terdeteksi (avg {token['avg_buys_per_maker']}x per wallet)"

    name_lower = token["name"].lower()
    symbol_lower = token["symbol"].lower()
    for kw in COPYCAT_KEYWORDS:
        if kw in name_lower or kw in symbol_lower:
            return False, f"Kemungkinan copycat ({kw})"

    if token["volume_24h"] > 0 and token["mcap"] > 0:
        vol_mcap_ratio = token["volume_24h"] / token["mcap"]
        if vol_mcap_ratio > 50:
            return False, f"Volume/MCap tidak wajar ({round(vol_mcap_ratio)}x)"

    return True, "OK"


# ─── SCORING ENGINE ──────────────────────────────────────────

def score_token(token):
    score = 0
    reasons = []
    warnings = []

    # [1] VOLUME MOMENTUM (25 pts)
    if token["volume_24h"] > 0:
        vol_ratio = token["volume_1h"] / max(token["volume_24h"] / 24, 1)
        if vol_ratio > 3:
            score += 25
            reasons.append(f"🔥 Volume spike ekstrem ({round(vol_ratio, 1)}x rata-rata)")
        elif vol_ratio > 2:
            score += 17
            reasons.append(f"📈 Volume 1h tinggi ({round(vol_ratio, 1)}x rata-rata)")
        elif vol_ratio > 1.5:
            score += 10
            reasons.append("📊 Volume 1h di atas rata-rata")

    # [2] BUY PRESSURE 1H (25 pts)
    bsr = token["h1_buy_sell_ratio"]
    if bsr > 3:
        score += 25
        reasons.append(f"💚 Buy pressure 1h sangat kuat ({round(bsr, 1)}x)")
    elif bsr > 2:
        score += 18
        reasons.append(f"✅ Buy dominan 1h ({round(bsr, 1)}x)")
    elif bsr > 1.4:
        score += 10
        reasons.append(f"🟡 Sedikit buy dominan ({round(bsr, 1)}x)")
    elif bsr < 0.7:
        score -= 20
        warnings.append("🔴 Sell pressure tinggi di 1 jam terakhir!")

    # [3] ORGANIC TRADING (20 pts)
    wash = token["wash_score"]
    makers = token["makers_24h"]
    if wash == 0 and makers > 500:
        score += 20
        reasons.append(f"👥 Trading organik ({makers} unique traders)")
    elif wash == 0 and makers > 200:
        score += 15
        reasons.append(f"👥 Trader cukup banyak ({makers} unique)")
    elif wash == 0 and makers > 100:
        score += 8
        reasons.append(f"👥 {makers} unique traders")
    elif wash == 1:
        score -= 5
        warnings.append(f"⚠️ Sedikit suspicious ({token['avg_buys_per_maker']}x avg per wallet)")
    elif wash == 2:
        score -= 15
        warnings.append("🔶 Wash trading suspicion tinggi!")

    # [4] LIQUIDITY HEALTH (15 pts)
    if token["mcap"] > 0:
        liq_ratio = token["liquidity_usd"] / token["mcap"]
        if liq_ratio > 0.15:
            score += 15
            reasons.append(f"💧 Liquidity sehat ({round(liq_ratio * 100, 1)}%)")
        elif liq_ratio > 0.08:
            score += 10
            reasons.append(f"💧 Liquidity cukup ({round(liq_ratio * 100, 1)}%)")
        elif liq_ratio < 0.03:
            score -= 10
            warnings.append("⚠️ Liquidity sangat tipis!")

    # [5] AGE SWEET SPOT (15 pts)
    age = token["age_hours"]
    if 3 <= age <= 12:
        score += 15
        reasons.append(f"⏰ Early gem zone ({age} jam)")
    elif 12 < age <= 24:
        score += 12
        reasons.append(f"⏰ Age ideal ({age} jam)")
    elif 24 < age <= 48:
        score += 6
        reasons.append(f"⏰ Masih dalam range ({age} jam)")
    elif age < 3:
        warnings.append(f"🆕 Sangat baru ({age} jam) - belum terbukti")
    elif age > 48:
        score -= 5
        warnings.append(f"📅 Sudah tua ({age} jam)")

    # BONUS momentum
    if token["price_change_1h"] > 30:
        score += 10
        reasons.append(f"🚀 +{round(token['price_change_1h'], 1)}% dalam 1 jam!")
    elif token["price_change_1h"] > 15:
        score += 5
        reasons.append(f"📈 +{round(token['price_change_1h'], 1)}% dalam 1 jam")

    # PENALTY dump
    if token["price_change_1h"] < -20:
        score -= 15
        warnings.append(f"📉 -{abs(round(token['price_change_1h'], 1))}% dalam 1 jam!")

    return score, reasons, warnings


def classify_signal(score):
    if score >= config.SCORE_MOONBAG:
        return "🌙 MOONBAG CANDIDATE", "Hold berminggu-minggu jika narrative kuat"
    elif score >= config.SCORE_SWING:
        return "🎯 SWING TARGET", "Hold 1-7 hari, pantau whale movement"
    elif score >= config.SCORE_SCALP:
        return "⚡ SCALP OPPORTUNITY", "Hold <2 jam, quick profit"
    else:
        return None, None


# ─── TELEGRAM MESSAGES ───────────────────────────────────────

async def send_alert(bot, token, score, signal_type, signal_desc, reasons, warnings):
    mcap_str = "${:,.0f}".format(token["mcap"])
    liq_str = "${:,.0f}".format(token["liquidity_usd"])
    vol_str = "${:,.0f}".format(token["volume_24h"])
    wash_label = ["✅ Organik", "🟡 Sedikit Suspicious", "🔶 Suspicious", "🔴 Wash Trading"][min(token["wash_score"], 3)]

    msg = f"""{signal_type}
━━━━━━━━━━━━━━━━━━━
🪙 *{token['name']}* (${token['symbol']})
📊 Score: *{score}/100*
💡 {signal_desc}

📈 *Market Data*
├ MCap: {mcap_str}
├ Liquidity: {liq_str}
├ Volume 24h: {vol_str}
├ Age: {token['age_hours']} jam
├ Buy/Sell 1h: {token['h1_buy_sell_ratio']}x
├ Unique Traders: {token['makers_24h']}
├ Trading: {wash_label}
└ Change 1h: {token['price_change_1h']}%

✅ *Kenapa Menarik:*
{chr(10).join(reasons)}"""

    if warnings:
        msg += f"\n\n⚠️ *Warning:*\n{chr(10).join(warnings)}"

    msg += f"\n\n🔗 [Chart & Trade]({token['pair_url']})"
    msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ─── SCAN LOGIC ──────────────────────────────────────────────

seen_tokens = set()


async def do_scan(bot, manual=False):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{timestamp}] {'Manual' if manual else 'Auto'} scan started...")

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
        if not token:
            continue

        token_key = token["address"] + str(round(token["mcap"] / 10000))
        if token_key in seen_tokens:
            continue

        passed, reason = passes_filter(token)
        if not passed:
            print(f"  FILTERED: {token['name']} - {reason}")
            filtered_count += 1
            continue

        score, reasons, warnings = score_token(token)
        signal_type, signal_desc = classify_signal(score)

        print(f"  {token['name']} | Score: {score} | MCap: ${token['mcap']:,.0f} | Makers: {token['makers_24h']}")

        if signal_type:
            seen_tokens.add(token_key)
            print(f"  >>> ALERT: {signal_type}")
            await send_alert(bot, token, score, signal_type, signal_desc, reasons, warnings)
            alerts_sent += 1
            await asyncio.sleep(1)

    if manual:
        summary = f"""✅ *Scan Selesai*
━━━━━━━━━━━━━━━━━━━
📊 Dipindai: {len(pairs)} token
🚫 Difilter: {filtered_count} token
🔔 Alert: {alerts_sent} sinyal
⏰ {datetime.now().strftime('%H:%M:%S')} WIB"""
        if alerts_sent == 0:
            summary += "\n\n💤 Belum ada koin yang memenuhi kriteria."
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=summary,
            parse_mode=ParseMode.MARKDOWN
        )

    if len(seen_tokens) > 500:
        seen_tokens.clear()

    print(f"Scan done. {alerts_sent} alerts, {filtered_count} filtered.")
    return alerts_sent


# ─── COMMAND HANDLERS ────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """🤖 *MEMECOIN MONITOR*
━━━━━━━━━━━━━━━━━━━
*Commands:*
/scan — Scan manual sekarang
/status — Cek status bot
/filter — Lihat filter aktif
/help — Tampilkan bantuan"""
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
        return
    await do_scan(context.bot, manual=True)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
        return
    msg = f"""✅ *Bot Status: ONLINE*
━━━━━━━━━━━━━━━━━━━
🕐 Auto scan: setiap {config.CHECK_INTERVAL_MINUTES} menit
📝 Cache: {len(seen_tokens)} token
⏰ {datetime.now().strftime('%H:%M:%S')} WIB

*Thresholds:*
🌙 Moonbag ≥{config.SCORE_MOONBAG} | 🎯 Swing ≥{config.SCORE_SWING} | ⚡ Scalp ≥{config.SCORE_SCALP}"""
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
        return
    msg = f"""🔍 *Filter Aktif*
━━━━━━━━━━━━━━━━━━━
💰 Min Liquidity: ${config.MIN_LIQUIDITY_USD:,}
📊 Min Volume 24h: ${config.MIN_VOLUME_24H:,}
⏰ Age: {config.MIN_AGE_HOURS}-{config.MAX_AGE_HOURS} jam
🛡️ Anti wash trading: ON
🚫 Anti copycat: ON
📉 Anti vol manipulasi: ON"""
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ─── BACKGROUND AUTO SCAN ────────────────────────────────────

async def background_scanner(bot):
    interval = config.CHECK_INTERVAL_MINUTES * 60
    while True:
        await asyncio.sleep(interval)
        print(f"\n[AUTO SCAN] {datetime.now().strftime('%H:%M:%S')}")
        await do_scan(bot, manual=False)


# ─── MAIN ────────────────────────────────────────────────────

async def main():
    print("=" * 50)
    print("  MEMECOIN MONITOR - Solana")
    print("=" * 50)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("help", cmd_help))

    await app.initialize()
    await app.start()

    bot = app.bot

    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text="""🤖 *MEMECOIN MONITOR v2 AKTIF*
━━━━━━━━━━━━━━━━━━━
✅ Anti wash trading: ON
✅ Anti copycat: ON
✅ Commands aktif

Ketik /scan untuk scan manual
Ketik /help untuk bantuan
⏳ Auto scan pertama dimulai...""",
        parse_mode=ParseMode.MARKDOWN
    )

    await do_scan(bot, manual=False)

    await asyncio.gather(
        app.updater.start_polling(drop_pending_updates=True),
        background_scanner(bot)
    )


if __name__ == "__main__":
    asyncio.run(main())
