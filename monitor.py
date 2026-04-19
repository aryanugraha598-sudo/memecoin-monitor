import requests
import time
import schedule
import asyncio
from datetime import datetime, timezone
from telegram import Bot
from telegram.constants import ParseMode
import config

# ─── DATA FETCHING ───────────────────────────────────────────

def get_pumpfun_graduated():
    """Ambil token Solana trending dari DexScreener"""
    all_pairs = []

    try:
        # Endpoint 1: Token profiles terbaru di Solana
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
                        pair_data = r_pair.json()
                        pairs = pair_data.get("pairs", [])
                        if pairs:
                            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                            all_pairs.append(best)
                    time.sleep(0.2)
                except:
                    continue
    except Exception as e:
        print(f"Error endpoint 1: {e}")

    try:
        # Endpoint 2: Boosted tokens
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
                        pair_data = r_pair.json()
                        pairs = pair_data.get("pairs", [])
                        if pairs:
                            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                            existing_addrs = [p.get("baseToken", {}).get("address") for p in all_pairs]
                            if best.get("baseToken", {}).get("address") not in existing_addrs:
                                all_pairs.append(best)
                    time.sleep(0.2)
                except:
                    continue
    except Exception as e:
        print(f"Error endpoint 2: {e}")

    try:
        # Endpoint 3: Trending Solana pairs langsung
        url3 = "https://api.dexscreener.com/latest/dex/search?q=solana"
        r3 = requests.get(url3, timeout=10)
        if r3.status_code == 200:
            data3 = r3.json()
            sol_pairs = [
                p for p in data3.get("pairs", [])
                if p.get("chainId") == "solana"
            ]
            print(f"  Direct Solana pairs found: {len(sol_pairs)}")
            for p in sol_pairs[:20]:
                existing_addrs = [x.get("baseToken", {}).get("address") for x in all_pairs]
                if p.get("baseToken", {}).get("address") not in existing_addrs:
                    all_pairs.append(p)
    except Exception as e:
        print(f"Error endpoint 3: {e}")

    print(f"Total pairs collected: {len(all_pairs)}")
    return all_pairs


def get_token_details(pair):
    """Extract detail dari pair data"""
    try:
        base = pair.get("baseToken", {})
        liquidity = pair.get("liquidity", {})
        volume = pair.get("volume", {})
        price_change = pair.get("priceChange", {})
        txns = pair.get("txns", {})

        created_at = pair.get("pairCreatedAt", 0)
        age_hours = 0
        if created_at:
            age_hours = (time.time() - created_at / 1000) / 3600

        h24_buys = txns.get("h24", {}).get("buys", 0)
        h24_sells = txns.get("h24", {}).get("sells", 0)
        buy_sell_ratio = h24_buys / max(h24_sells, 1)

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
            "buy_sell_ratio": round(buy_sell_ratio, 2),
            "h24_buys": h24_buys,
            "h24_sells": h24_sells,
            "pair_url": pair.get("url", ""),
        }
    except Exception as e:
        print(f"Error parsing pair: {e}")
        return None


# ─── SCORING ENGINE ──────────────────────────────────────────

def score_token(token):
    score = 0
    reasons = []
    warnings = []

    # [1] VOLUME MOMENTUM (30 pts)
    vol_ratio = token["volume_1h"] / max(token["volume_24h"] / 24, 1)
    if vol_ratio > 3:
        score += 30
        reasons.append("🔥 Volume 1h spike ekstrem ({}x rata-rata)".format(round(vol_ratio, 1)))
    elif vol_ratio > 2:
        score += 20
        reasons.append("📈 Volume 1h tinggi ({}x rata-rata)".format(round(vol_ratio, 1)))
    elif vol_ratio > 1.5:
        score += 10
        reasons.append("📊 Volume 1h di atas rata-rata")

    # [2] BUY/SELL RATIO (25 pts)
    bsr = token["buy_sell_ratio"]
    if bsr > 2.5:
        score += 25
        reasons.append("💚 Buy pressure sangat kuat ({} buys vs sells)".format(round(bsr, 1)))
    elif bsr > 1.8:
        score += 18
        reasons.append("✅ Buy dominan ({})".format(round(bsr, 1)))
    elif bsr > 1.3:
        score += 10
        reasons.append("🟡 Sedikit buy dominan ({})".format(round(bsr, 1)))
    elif bsr < 0.8:
        score -= 15
        warnings.append("🔴 Sell pressure tinggi!")

    # [3] LIQUIDITY RATIO (25 pts)
    if token["mcap"] > 0:
        liq_ratio = token["liquidity_usd"] / token["mcap"]
        if liq_ratio > 0.15:
            score += 25
            reasons.append("💧 Liquidity sangat sehat ({}%)".format(round(liq_ratio * 100, 1)))
        elif liq_ratio > 0.08:
            score += 15
            reasons.append("💧 Liquidity cukup ({}%)".format(round(liq_ratio * 100, 1)))
        elif liq_ratio < 0.03:
            score -= 10
            warnings.append("⚠️ Liquidity tipis, rugpull risk!")

    # [4] AGE SWEET SPOT (20 pts)
    age = token["age_hours"]
    if 2 <= age <= 24:
        score += 20
        reasons.append("⏰ Age ideal ({} jam) - early tapi sudah proven".format(age))
    elif 24 < age <= 48:
        score += 12
        reasons.append("⏰ Age masih bagus ({} jam)".format(age))
    elif age < 2:
        score += 5
        warnings.append("🆕 Sangat baru ({} jam) - high risk/high reward".format(age))
    elif age > 48:
        score -= 5
        warnings.append("📅 Sudah agak tua ({} jam)".format(age))

    # BONUS: Price momentum
    if token["price_change_1h"] > 20:
        score += 10
        reasons.append("🚀 +{}% dalam 1 jam terakhir".format(round(token["price_change_1h"], 1)))

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


# ─── FILTER ENGINE ───────────────────────────────────────────

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
    return True, "OK"


# ─── TELEGRAM ALERT ──────────────────────────────────────────

async def send_alert(token, score, signal_type, signal_desc, reasons, warnings):
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    mcap_str = "${:,.0f}".format(token["mcap"])
    liq_str = "${:,.0f}".format(token["liquidity_usd"])
    vol_str = "${:,.0f}".format(token["volume_24h"])

    msg = f"""
{signal_type}
━━━━━━━━━━━━━━━━━━━
🪙 *{token['name']}* (${token['symbol']})
📊 Score: *{score}/100*
💡 {signal_desc}

📈 *Market Data*
├ MCap: {mcap_str}
├ Liquidity: {liq_str}
├ Volume 24h: {vol_str}
├ Age: {token['age_hours']} jam
├ Buy/Sell: {token['buy_sell_ratio']}x
└ Change 1h: {token['price_change_1h']}%

✅ *Kenapa Menarik:*
{chr(10).join(reasons)}
"""

    if warnings:
        msg += f"\n⚠️ *Warning:*\n{chr(10).join(warnings)}"

    msg += f"\n\n🔗 [Chart & Trade]({token['pair_url']})"
    msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


async def send_startup_message():
    """Kirim pesan saat bot pertama kali nyala"""
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    msg = """
🤖 *MEMECOIN MONITOR AKTIF*
━━━━━━━━━━━━━━━━━━━
✅ Bot berhasil dinyalakan
🔍 Scanning Solana tokens setiap 15 menit
📊 Filter aktif:
├ Min Liquidity: $30,000
├ Min Volume 24h: $50,000
├ Age: 1-72 jam
└ Score minimum: 50/100

⏳ Scan pertama sedang berjalan...
"""
    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN
    )


# ─── MAIN LOOP ───────────────────────────────────────────────

seen_tokens = set()


async def run_scan():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning Solana tokens...")

    pairs = get_pumpfun_graduated()
    print(f"Found {len(pairs)} pairs total")

    alerts_sent = 0

    for pair in pairs:
        token = get_token_details(pair)
        if not token:
            continue

        # Skip duplikat
        token_key = token["address"] + str(round(token["mcap"] / 10000))
        if token_key in seen_tokens:
            continue

        # Filter
        passed, reason = passes_filter(token)
        if not passed:
            print(f"  FILTERED: {token['name']} - {reason}")
            continue

        # Score
        score, reasons, warnings = score_token(token)
        signal_type, signal_desc = classify_signal(score)

        print(f"  {token['name']} | Score: {score} | MCap: ${token['mcap']:,.0f} | Age: {token['age_hours']}h")

        if signal_type:
            seen_tokens.add(token_key)
            print(f"  >>> ALERT SENT: {signal_type}")
            await send_alert(token, score, signal_type, signal_desc, reasons, warnings)
            alerts_sent += 1
            await asyncio.sleep(1)

    print(f"Scan complete. {alerts_sent} alerts sent.")

    # Bersihkan seen_tokens kalau terlalu besar
    if len(seen_tokens) > 500:
        seen_tokens.clear()
        print("Cleared seen_tokens cache")


def run_async_scan():
    asyncio.run(run_scan())


def main():
    print("=" * 50)
    print("  MEMECOIN MONITOR - Solana")
    print("=" * 50)
    print(f"Check interval: every {config.CHECK_INTERVAL_MINUTES} minutes")
    print("Sending startup message to Telegram...")

    # Kirim pesan startup ke Telegram
    asyncio.run(send_startup_message())

    print("Starting first scan...")
    run_async_scan()

    # Schedule scan berikutnya
    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(run_async_scan)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
