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
    """Ambil koin yang sudah graduate dari pump.fun via DexScreener"""
    try:
        url = "https://api.dexscreener.com/latest/dex/search?q=pump"
        r = requests.get(url, timeout=10)
        data = r.json()
        pairs = data.get("pairs", [])
        
        # Filter hanya Solana + dari pump.fun
        sol_pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and "pump" in p.get("url", "").lower()
            and p.get("dexId") in ["raydium", "pumpfun"]
        ]
        return sol_pairs
    except Exception as e:
        print(f"Error fetching pairs: {e}")
        return []

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
    msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S WIB')}"
    
    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

# ─── MAIN LOOP ───────────────────────────────────────────────

seen_tokens = set()

async def run_scan():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning pump.fun graduated tokens...")
    
    pairs = get_pumpfun_graduated()
    print(f"Found {len(pairs)} pairs from pump.fun")
    
    alerts_sent = 0
    
    for pair in pairs:
        token = get_token_details(pair)
        if not token:
            continue
        
        # Skip yang sudah pernah di-alert
        token_key = token["address"] + str(round(token["mcap"] / 10000))
        if token_key in seen_tokens:
            continue
        
        # Filter
        passed, reason = passes_filter(token)
        if not passed:
            continue
        
        # Score
        score, reasons, warnings = score_token(token)
        signal_type, signal_desc = classify_signal(score)
        
        if signal_type:
            seen_tokens.add(token_key)
            print(f"  ALERT: {token['name']} | Score: {score} | {signal_type}")
            
            await send_alert(token, score, signal_type, signal_desc, reasons, warnings)
            alerts_sent += 1
            await asyncio.sleep(1)  # Rate limit telegram
    
    print(f"Scan complete. {alerts_sent} alerts sent.")

def run_async_scan():
    asyncio.run(run_scan())

def main():
    print("=" * 50)
    print("  MEMECOIN MONITOR - Solana/PumpFun")
    print("=" * 50)
    print(f"Check interval: every {config.CHECK_INTERVAL_MINUTES} minutes")
    print("Starting first scan...")
    
    # Scan pertama langsung
    run_async_scan()
    
    # Schedule berikutnya
    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(run_async_scan)
    
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
