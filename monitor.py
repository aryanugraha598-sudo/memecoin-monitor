"""
MEMECOIN MONITOR v12
Upgrade dari v11:

LAPISAN 1 — HARD RUG FILTER (sebelum scoring):
  - Bundler % detection via GMGN top traders
  - Top3/Top10 holder concentration hard reject
  - Dev hold % check — reject kalau >20%
  - Wash trading level 3 hard reject (sebelumnya hanya penalti)
  - Liq/MCap ratio minimum

LAPISAN 2 — SMART MONEY SIGNAL UPGRADE:
  - SM bonus sekarang di-weighted (1.5x multiplier)
  - Fresh wallet whale detection di alert
  - Insider wallet summary lebih detail

LAPISAN 3 — DEAD COIN REVIVAL SCANNER:
  - Endpoint baru: DexScreener trending gainers untuk tangkap koin lama
  - Scan token age >48 jam dengan volume spike mendadak
  - Revival pass filter bypass untuk downtrend filter
  - Deteksi LP increase vs decrease (revival legit vs dump)

LAPISAN 4 — SCORING OVERHAUL:
  - Weighted scoring (bukan flat addition)
  - Rug score system (0-100) sebagai second opinion
  - Penalty lebih tegas untuk sinyal bahaya
  - Revival score tidak dikill oleh age penalty
"""

import requests
import time
import asyncio
import re
import json
import os
import websockets
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import config

# ══════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════
volume_history      = {}
seen_addresses      = {}
hold_watchlist      = set()
auto_watchlist      = {}
sm_wallets          = []
sm_last_buy         = {}
hold_vol_prev       = {}
pumpfun_bc          = {}
boosted_tokens      = set()
ws_alerted          = {}
aw_pump_alerted     = {}
# v12: track revival alerts agar tidak spam
revival_alerted     = set()
# v12: cache liq history untuk deteksi LP increase/decrease
liq_history         = {}

SEEN_TTL_HOURS = 6
STATE_FILE     = "state.json"
AW_DEAD_MCAP   = 3000


# ══════════════════════════════════════════════════════════════
#  PERSISTENT STATE
# ══════════════════════════════════════════════════════════════

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "seen":            seen_addresses,
                "vol_hist":        {k: list(v) for k, v in volume_history.items()},
                "liq_hist":        {k: list(v) for k, v in liq_history.items()},
                "hold":            list(hold_watchlist),
                "auto_wl":         auto_watchlist,
                "sm_wallets":      sm_wallets,
                "sm_last_buy":     sm_last_buy,
                "pumpfun_bc":      pumpfun_bc,
                "ws_alerted":      ws_alerted,
                "aw_pump_alerted": aw_pump_alerted,
                "revival_alerted": list(revival_alerted),
            }, f)
    except Exception as e:
        print(f"  save_state err: {e}")

def load_state():
    global seen_addresses, volume_history, liq_history, hold_watchlist
    global auto_watchlist, sm_wallets, sm_last_buy
    global pumpfun_bc, ws_alerted, aw_pump_alerted, revival_alerted
    sm_wallets.clear()
    sm_wallets.extend(config.SMART_MONEY_WALLETS)
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        seen_addresses  = d.get("seen", {})
        volume_history  = {k: [(t, v) for t, v in vs]
                           for k, vs in d.get("vol_hist", {}).items()}
        liq_history     = {k: [(t, v) for t, v in vs]
                           for k, vs in d.get("liq_hist", {}).items()}
        hold_watchlist  = set(d.get("hold", []))
        auto_watchlist  = d.get("auto_wl", {})
        sm_last_buy     = d.get("sm_last_buy", {})
        pumpfun_bc      = d.get("pumpfun_bc", {})
        ws_alerted      = d.get("ws_alerted", {})
        aw_pump_alerted = d.get("aw_pump_alerted", {})
        revival_alerted = set(d.get("revival_alerted", []))
        for w in d.get("sm_wallets", []):
            if w not in sm_wallets:
                sm_wallets.append(w)
        print(f"  State loaded: {len(seen_addresses)} seen | "
              f"{len(hold_watchlist)} hold | {len(auto_watchlist)} auto_wl | "
              f"{len(sm_wallets)} SM wallets")
    except Exception as e:
        print(f"  load_state err: {e}")


# ══════════════════════════════════════════════════════════════
#  TRACKING HELPERS
# ══════════════════════════════════════════════════════════════

def track_volume(addr, vol_1h):
    if addr not in volume_history:
        volume_history[addr] = []
    volume_history[addr].append((time.time(), vol_1h))
    volume_history[addr] = volume_history[addr][-24:]  # simpan 24 snapshot

def track_liq(addr, liq):
    if addr not in liq_history:
        liq_history[addr] = []
    liq_history[addr].append((time.time(), liq))
    liq_history[addr] = liq_history[addr][-24:]

def get_liq_trend(addr):
    """
    Return: 'increasing', 'decreasing', 'stable', 'unknown'
    Penting untuk bedakan revival legit vs dead cat bounce.
    """
    hist = liq_history.get(addr, [])
    if len(hist) < 3:
        return "unknown"
    recent  = [v for _, v in hist[-3:]]
    earlier = [v for _, v in hist[:3]]
    avg_r = sum(recent)  / len(recent)
    avg_e = sum(earlier) / len(earlier)
    if avg_e <= 0:
        return "unknown"
    ratio = avg_r / avg_e
    if ratio > 1.10:
        return "increasing"
    elif ratio < 0.90:
        return "decreasing"
    return "stable"


# ══════════════════════════════════════════════════════════════
#  LAPISAN 1 — RUG FILTER (HARD REJECT)
# ══════════════════════════════════════════════════════════════

def calculate_rug_score(token, holder_data=None, gmgn_traders=None):
    """
    Hitung rug score 0-100.
    Makin tinggi = makin berbahaya.
    Dipakai sebagai SECOND OPINION setelah filter dasar.
    """
    score = 0

    # 1. Wash trading
    if token["wash"] == 3:
        score += 35
    elif token["wash"] == 2:
        score += 20
    elif token["wash"] == 1:
        score += 10

    # 2. Liquidity sangat tipis vs MCap
    if token["mcap"] > 0:
        liq_ratio = token["liq"] / token["mcap"]
        if liq_ratio < 0.02:
            score += 25
        elif liq_ratio < 0.04:
            score += 12
        elif liq_ratio < config.LIQ_MCAP_MIN_RATIO:
            score += 6

    # 3. Liquidity trend menurun (LP sedang ditarik)
    liq_trend = get_liq_trend(token["address"])
    if liq_trend == "decreasing":
        score += 20

    # 4. Holder concentration dari GMGN traders
    if gmgn_traders:
        # Cek apakah ada banyak wallet dengan tag bundler/sniper
        bundler_count = sum(
            1 for tr in gmgn_traders
            if any(t in ["sniper", "bundler"] for t in (tr.get("tags") or []))
        )
        if bundler_count >= 5:
            score += 25
        elif bundler_count >= 3:
            score += 15

    # 5. Token sangat baru dengan volume sangat tinggi (pump & dump setup)
    if token["age_h"] < 2 and token["v1h"] > 100000:
        score += 15

    # 6. Buy/sell ratio terlalu sempurna (manipulasi)
    # BSR > 10 di token baru = aneh, kemungkinan koordinasi
    if token["age_h"] < 6 and token["h1_bsr"] > 10:
        score += 10

    # 7. Volume/MCap ratio ekstrem
    if token["mcap"] > 0 and token["v24h"] > token["mcap"] * 50:
        score += 15

    return min(score, 100)


def check_rug_filter(token, holder_ok, top3_pct, top10_pct, gmgn_traders=None):
    """
    LAPISAN 1: Hard reject berdasarkan sinyal rug.
    Return: (passed: bool, reason: str, rug_score: int)
    """
    if not config.ENABLE_RUG_FILTER:
        return True, "OK", 0

    # Hard reject 1: Wash trading level 3
    if token["wash"] >= config.WASH_HARD_REJECT:
        return False, f"RUG: Wash trading ekstrem ({token['abpm']}x/wallet)", 100

    # Hard reject 2: Holder sangat terkonsentrasi
    if top3_pct > 0 and top3_pct > config.TOP3_HOLDER_MAX_PCT:
        return False, f"RUG: Top3 holder {round(top3_pct,1)}% supply", 90

    if top10_pct > 0 and top10_pct > config.TOP10_HOLDER_MAX_PCT:
        return False, f"RUG: Top10 holder {round(top10_pct,1)}% supply", 85

    # Hard reject 3: LP sangat tipis untuk MCap yang ada
    if token["mcap"] > 50000 and token["liq"] > 0:
        liq_ratio = token["liq"] / token["mcap"]
        if liq_ratio < 0.015:
            return False, f"RUG: Liq/MCap {round(liq_ratio*100,2)}% (< 1.5%)", 80

    # Hard reject 4: LP sedang aktif ditarik + harga drop
    liq_trend = get_liq_trend(token["address"])
    if liq_trend == "decreasing" and token["pc_1h"] < -15:
        return False, f"RUG: LP ditarik + dump {token['pc_1h']}% 1h", 90

    # Hitung rug score untuk warning
    rug_score = calculate_rug_score(token, None, gmgn_traders)
    if rug_score >= config.RUG_SCORE_REJECT:
        return False, f"RUG Score: {rug_score}/100 (terlalu tinggi)", rug_score

    return True, "OK", rug_score


# ══════════════════════════════════════════════════════════════
#  AUTO WATCHLIST HELPERS
# ══════════════════════════════════════════════════════════════

def add_to_auto_watchlist(token, score, sig, ttype):
    addr = token["address"]
    if addr in auto_watchlist:
        return
    if len(auto_watchlist) >= config.AUTO_WL_MAX:
        oldest = sorted(auto_watchlist.items(), key=lambda x: x[1].get("ts", 0))
        del auto_watchlist[oldest[0][0]]
    auto_watchlist[addr] = {
        "name":      token["name"],
        "symbol":    token["symbol"],
        "score0":    score,
        "mcap0":     token["mcap"],
        "v1h0":      token["v1h"],
        "liq0":      token["liq"],
        "pc_1h0":    token["pc_1h"],
        "bsr0":      token["h1_bsr"],
        "ts":        time.time(),
        "sig":       sig,
        "ttype":     ttype,
        "pair_url":  token["pair_url"],
        "mcap_now":  token["mcap"],
        "v1h_now":   token["v1h"],
        "score_now": score,
        "status":    "HOLD",
        "check_ts":  time.time(),
    }
    print(f"  [AW] Added: {token['name']} score={score} ({sig})")

def cleanup_auto_watchlist():
    now   = time.time()
    stale = []
    for addr, d in auto_watchlist.items():
        age_h    = (now - d.get("ts", 0)) / 3600
        mcap_now = d.get("mcap_now", 999999)
        if (age_h > config.AUTO_WL_MAX_AGE_H
                or d.get("status") == "EXITED"
                or (mcap_now < AW_DEAD_MCAP and mcap_now > 0)):
            stale.append(addr)
    for addr in stale:
        del auto_watchlist[addr]
        aw_pump_alerted.pop(addr, None)
    if stale:
        print(f"  [AW] Cleaned {len(stale)} stale entries")

def get_aw_status(score_now, eu):
    if eu >= 3:
        return "EXIT", "🔴", "EXIT SEKARANG!"
    elif eu >= 2:
        return "EXIT_WARN", "🔶", "Pertimbangkan exit"
    elif score_now >= config.AW_STATUS_HOLD:
        return "HOLD", "🟢", "Hold lanjut"
    elif score_now >= config.AW_STATUS_WEAK:
        return "WEAK", "🟡", "Momentum melemah"
    else:
        return "EXIT", "🔴", "Skor terlalu rendah — pertimbangkan exit"


# ══════════════════════════════════════════════════════════════
#  TTL DEDUP
# ══════════════════════════════════════════════════════════════

def is_seen(addr):
    if addr not in seen_addresses:
        return False
    if (time.time() - seen_addresses[addr]) / 3600 > SEEN_TTL_HOURS:
        del seen_addresses[addr]
        return False
    return True

def mark_seen(addr):
    seen_addresses[addr] = time.time()

def cleanup_seen():
    now   = time.time()
    stale = [k for k, t in seen_addresses.items()
             if (now - t) / 3600 > SEEN_TTL_HOURS]
    for k in stale:
        del seen_addresses[k]


# ══════════════════════════════════════════════════════════════
#  VOLUME TRACKING & RESURRECTION
# ══════════════════════════════════════════════════════════════

def detect_resurrection(addr, cur_vol):
    hist = volume_history.get(addr, [])
    if len(hist) >= 3:
        prev = [v for _, v in hist[:-1]]
        avg  = sum(prev) / len(prev)
        mult = cur_vol / max(avg, 1)
        if (avg < config.RESURRECTION_AVG_THRESHOLD
                and cur_vol > config.RESURRECTION_VOL_SPIKE
                and mult >= config.RESURRECTION_MIN_MULT):
            return True, round(mult)
    if addr in auto_watchlist:
        aw   = auto_watchlist[addr]
        v1h0 = aw.get("v1h0", 0)
        if v1h0 > 0 and cur_vol > v1h0 * 3 and cur_vol > 1000:
            mult = round(cur_vol / max(v1h0, 1))
            return True, mult
    return False, 0


# ══════════════════════════════════════════════════════════════
#  LAPISAN 3 — DEAD COIN REVIVAL SCANNER
# ══════════════════════════════════════════════════════════════

def detect_revival_signal(token):
    """
    Deteksi koin lama yang tiba-tiba hidup kembali.
    Berbeda dari resurrection — ini untuk token yang belum pernah
    masuk history volume bot (di luar radar sebelumnya).

    Return: (is_revival: bool, confidence: str, notes: list)
    """
    if not config.ENABLE_REVIVAL_SCAN:
        return False, "", []

    addr    = token["address"]
    age_h   = token["age_h"]
    v1h     = token["v1h"]
    v6h     = token["v6h"]
    v24h    = token["v24h"]
    pc_1h   = token["pc_1h"]
    pc_6h   = token["pc_6h"]
    pc_24h  = token["pc_24h"]
    liq     = token["liq"]
    mcap    = token["mcap"]
    bsr     = token["h1_bsr"]

    notes = []

    # Syarat minimum: token cukup tua
    if age_h < config.REVIVAL_MIN_TOKEN_AGE_H:
        return False, "", []

    # Syarat minimum: ada volume berarti sekarang
    if v1h < config.REVIVAL_MIN_VOL_1H:
        return False, "", []

    # Deteksi pola volume: v1h jauh lebih tinggi dari average harian
    # v24h / 24 = avg per jam. Kalau v1h >> avg, ada spike
    avg_per_hour = v24h / 24 if v24h > 0 else 0
    # Kalau avg_per_hour rendah tapi v1h tinggi = spike mendadak
    vol_spike_mult = v1h / max(avg_per_hour, 10)

    if vol_spike_mult < config.DEAD_COIN_VOL_MULTIPLIER:
        return False, "", []

    # Cek dari history volume bot
    hist = volume_history.get(addr, [])
    was_dormant = False
    if len(hist) >= 3:
        prev_vols = [v for _, v in hist[:-1]]
        avg_prev  = sum(prev_vols) / len(prev_vols)
        if avg_prev < config.RESURRECTION_AVG_THRESHOLD and v1h > avg_prev * config.REVIVAL_VOL_SPIKE_MULT:
            was_dormant = True
            notes.append(f"📊 Vol spike {round(v1h/max(avg_prev,1))}x dari history bot")
    else:
        # Tidak ada history = pertama kali terdeteksi padahal token tua
        # Ini adalah kasus Strawberita — muncul tiba-tiba dengan volume besar
        if age_h > 72 and v1h > config.REVIVAL_MIN_VOL_1H:
            was_dormant = True
            notes.append(f"👁 Token {round(age_h/24,1)} hari baru terdeteksi dengan vol tinggi")

    if not was_dormant:
        return False, "", []

    # Cek LP trend — revival legit = LP bertambah atau stabil
    liq_trend = get_liq_trend(addr)
    if liq_trend == "decreasing":
        # LP ditarik = dead cat bounce, bukan revival
        notes.append("⚠️ LP sedang ditarik — hati-hati dead cat bounce!")
        confidence = "LOW"
    elif liq_trend == "increasing":
        notes.append("✅ LP bertambah — revival terlihat organik")
        confidence = "HIGH"
    else:
        notes.append("🟡 LP stabil/belum ada data LP history")
        confidence = "MEDIUM"

    # Cek price momentum: revival valid biasanya ada kenaikan 1h
    if pc_1h > 20:
        notes.append(f"🚀 +{pc_1h}% dalam 1 jam — momentum kuat")
        if confidence == "MEDIUM":
            confidence = "HIGH"
    elif pc_1h > 5:
        notes.append(f"📈 +{pc_1h}% dalam 1 jam")
    elif pc_1h < -10:
        notes.append(f"⚠️ -{abs(pc_1h)}% 1h — harga masih turun meski vol naik")
        if confidence == "HIGH":
            confidence = "MEDIUM"

    # BSR check
    if bsr > 2:
        notes.append(f"💚 BSR {bsr}x — buyer dominan")
    elif bsr < 0.8:
        notes.append(f"🔴 BSR {bsr}x — seller dominan (distribusi)")
        if confidence != "LOW":
            confidence = "LOW"

    # Liq/MCap health
    if mcap > 0:
        lr = liq / mcap
        if lr < 0.03:
            notes.append("⚠️ Liq tipis vs MCap")

    return True, confidence, notes


def get_revival_score_bonus(confidence):
    """Score bonus berdasarkan confidence level revival."""
    return {"HIGH": 40, "MEDIUM": 25, "LOW": 10}.get(confidence, 0)


# ══════════════════════════════════════════════════════════════
#  PUMP.FUN WEBSOCKET
# ══════════════════════════════════════════════════════════════

pumpfun_queue = asyncio.Queue()

async def pumpfun_websocket_listener():
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            print(f"  [WS] Connecting pump.fun...")
            async with websockets.connect(
                    uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeTokenTrade"}))
                print(f"  [WS] Connected!")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        await process_pumpfun_trade(msg)
                    except Exception as e:
                        print(f"  [WS] msg err: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            print(f"  [WS] Closed: {e}. Retry 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"  [WS] Err: {e}. Retry 10s...")
            await asyncio.sleep(10)

async def process_pumpfun_trade(msg):
    tx_type = msg.get("txType", "")
    if tx_type not in ("buy", "sell"):
        return
    mint   = msg.get("mint", "")
    if not mint:
        return
    name   = msg.get("name", "Unknown")
    symbol = msg.get("symbol", "???")
    v_sol  = msg.get("vSolInBondingCurve", 0) or 0
    GRAD   = 85.0
    bc_pct = min(round((v_sol / GRAD) * 100, 1), 99.9) if v_sol > 0 else 0
    pumpfun_bc[mint] = bc_pct
    if v_sol < config.PUMPFUN_MIN_SOL:
        return
    prev = ws_alerted.get(mint, 0)
    if bc_pct >= config.PUMPFUN_BC_ALERT_PCT and prev < config.PUMPFUN_BC_ALERT_PCT:
        ws_alerted[mint] = config.PUMPFUN_BC_ALERT_PCT
        await pumpfun_queue.put({
            "type": "PRE_GRADUATION", "mint": mint, "name": name,
            "symbol": symbol, "v_sol": v_sol, "bc_pct": bc_pct, "ts": time.time(),
        })
    elif bc_pct >= config.PUMPFUN_BC_MIN_PCT and prev < config.PUMPFUN_BC_MIN_PCT:
        ws_alerted[mint] = config.PUMPFUN_BC_MIN_PCT
        await pumpfun_queue.put({
            "type": "BC_MILESTONE", "mint": mint, "name": name,
            "symbol": symbol, "v_sol": v_sol, "bc_pct": bc_pct, "ts": time.time(),
        })

async def pumpfun_alert_processor(bot):
    print("  [WS] Alert processor ready")
    while True:
        try:
            item = await asyncio.wait_for(pumpfun_queue.get(), timeout=60)
        except asyncio.TimeoutError:
            continue
        try:
            mint   = item["mint"]
            bc_pct = item["bc_pct"]
            v_sol  = item["v_sol"]
            name   = item.get("name", "Unknown")
            symbol = item.get("symbol", "???")
            itype  = item["type"]
            pair, all_urls = fetch_token_best_pair(mint)
            token  = get_token_details(pair, all_urls) if pair else None
            if itype == "PRE_GRADUATION":
                header = "🚀 PRE-GRADUATION ALERT"
                desc   = f"BC {bc_pct}% — Graduation SEBENTAR LAGI!"
                note   = "WINDOW SEMPIT — pump 2-5x saat graduation biasa"
            else:
                header = "📈 BC MILESTONE"
                desc   = f"BC {bc_pct}% — Mendekati graduation"
                note   = "Pantau, mendekati graduation threshold"
            gb, gs, gi = 0, None, []
            if config.ENABLE_GMGN_SMART_MONEY and pair:
                gb, gs, gi = gmgn_check_smart_money(mint, name)
            msg = (
                f"{header}\n━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{name}* (${symbol})\n"
                f"CA: `{mint}`\n"
                f"💡 {desc}\n_{note}_\n\n"
                f"⛽ BC: *{bc_pct}%* | SOL: *{round(v_sol,1)}* / 85\n"
                f"Sisa: ~{round(85-v_sol,1)} SOL\n"
            )
            if token:
                msg += (f"\nMCap: ${token['mcap']:,.0f} | "
                        f"Vol1h: ${token['v1h']:,.0f} | BSR: {token['h1_bsr']}x\n")
            if gs:
                msg += f"\n🧠 SM: {gs}\n"
            msg += f"\n🔗 [Pump.fun](https://pump.fun/{mint})"
            if token:
                msg += f" | [Chart]({token['pair_url']})"
            msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{mint})"
            msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID, text=msg,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        except Exception as e:
            print(f"  [WS] processor err: {e}")
        await asyncio.sleep(2)


# ══════════════════════════════════════════════════════════════
#  HOLDER DISTRIBUTION
# ══════════════════════════════════════════════════════════════

def check_holder_distribution(token_address):
    """
    Return: (ok, top10_pct, top3_pct, warn_msg)
    v12: juga return top3_pct untuk rug filter
    """
    if not config.ENABLE_HOLDER_CHECK:
        return True, 0, 0, None

    holders = []
    try:
        headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}
        r = requests.get(
            f"https://api.solscan.io/v2/token/holders"
            f"?address={token_address}&page=1&page_size=10",
            headers=headers, timeout=6)
        if r.status_code == 200:
            data    = r.json().get("data", {})
            holders = data.get("items", []) if isinstance(data, dict) else data
    except Exception:
        pass

    if not holders:
        try:
            r = requests.get(
                "https://public-api.solscan.io/token/holders",
                params={"tokenAddress": token_address, "limit": 10, "offset": 0},
                headers={"User-Agent": "Mozilla/5.0", "accept": "application/json"},
                timeout=6)
            if r.status_code == 200:
                holders = r.json().get("data", [])
        except Exception:
            pass

    if not holders:
        return True, 0, 0, None

    BURN = {
        "11111111111111111111111111111111",
        "So11111111111111111111111111111111111111112",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    }
    try:
        amounts = []
        for h in holders[:10]:
            owner  = h.get("owner", h.get("address", ""))
            amount = float(h.get("amount", h.get("uiAmount", 0)) or 0)
            if owner not in BURN and amount > 0:
                amounts.append(amount)
        total = sum(amounts)
        if total <= 0:
            return True, 0, 0, None

        top10_pct = (sum(amounts[:10]) / total * 100) if amounts else 0
        top3_pct  = (sum(amounts[:3])  / total * 100) if len(amounts) >= 3 else top10_pct

        # Evaluasi
        if top3_pct > config.TOP3_HOLDER_MAX_PCT:
            ok   = False
            warn = f"🔴 Top3 holder ekstrem ({round(top3_pct,1)}% supply)"
        elif top10_pct > config.TOP10_HOLDER_MAX_PCT:
            ok   = False
            warn = f"🔴 Top10 holder terkonsentrasi ({round(top10_pct,1)}% supply)"
        elif top3_pct > 40:
            ok   = True
            warn = f"🟡 Top3 holder sedang ({round(top3_pct,1)}%) — pantau"
        else:
            ok   = True
            warn = None

        return ok, round(top10_pct, 1), round(top3_pct, 1), warn

    except Exception as e:
        print(f"  Holder calc err: {e}")
        return True, 0, 0, None


# ══════════════════════════════════════════════════════════════
#  GMGN SMART MONEY
# ══════════════════════════════════════════════════════════════

GMGN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept":     "application/json",
    "Referer":    "https://gmgn.ai/",
}

def gmgn_get_top_traders(addr, limit=20):
    try:
        r = requests.get(
            f"https://gmgn.ai/defi/quotation/v1/tokens/top_traders/sol/{addr}",
            headers=GMGN_HEADERS, params={"limit": limit}, timeout=8)
        if r.status_code != 200:
            return []
        return r.json().get("data", {}).get("items", [])
    except Exception as e:
        print(f"  GMGN err: {e}")
        return []

def gmgn_check_smart_money(addr, name=""):
    if not config.ENABLE_GMGN_SMART_MONEY:
        return 0, None, [], []
    traders = gmgn_get_top_traders(addr)
    if not traders:
        return 0, None, [], traders
    total_bonus = 0
    found       = []
    insiders    = []
    for tr in traders:
        tags = [str(t).lower().strip() for t in (tr.get("tags") or [])]
        for label, bonus in config.GMGN_LABEL_SCORES.items():
            if label in tags:
                total_bonus += bonus
                found.append(label)
                w = tr.get("address", "")
                if w:
                    insiders.append({
                        "wallet": w, "label": label,
                        "pnl": tr.get("realized_profit", 0) or 0,
                    })
                break
    total_bonus = min(total_bonus, config.GMGN_MAX_BONUS)
    # v12: apply weighted multiplier jika SM signal kuat
    if total_bonus >= config.SM_BOOST_MIN:
        total_bonus = int(total_bonus * config.SCORE_WEIGHT_SM)
        total_bonus = min(total_bonus, 75)  # cap setelah weighted
    if not found:
        return 0, None, [], traders
    emap = {
        "insider": "🔴 Insider", "smart_degen": "🧠 SmDegen",
        "kol": "📢 KOL", "sniper": "🎯 Sniper",
        "whale": "🐋 Whale", "smart": "🧠 Smart",
        "early_buyer": "🟢 Early",
    }
    counts  = {}
    for l in found:
        counts[l] = counts.get(l, 0) + 1
    parts   = [f"{emap.get(l,l)} x{c}" for l, c in counts.items()]
    summary = " | ".join(parts) + f" (+{total_bonus}pts)"
    print(f"  🧠 GMGN [{name}]: {', '.join(found)}")
    return total_bonus, summary, insiders, traders


# ══════════════════════════════════════════════════════════════
#  TWITTER / NITTER
# ══════════════════════════════════════════════════════════════

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.lucahammer.com",
]

def analyze_twitter(name, symbol, address):
    if not config.ENABLE_TWITTER_CHECK:
        return 0, None
    headers = {"User-Agent": "Mozilla/5.0"}
    tweets  = []
    for query in [f"${symbol}", address[:20]]:
        for inst in NITTER_INSTANCES:
            try:
                r = requests.get(
                    f"{inst}/search?q={requests.utils.quote(query)}&f=tweets",
                    headers=headers, timeout=5)
                if r.status_code != 200:
                    continue
                for m in re.findall(
                        r'class="tweet-content[^"]*"[^>]*>(.*?)</div>',
                        r.text, re.DOTALL)[:4]:
                    c = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', m)).strip()
                    if len(c) > 10:
                        tweets.append(c)
                if tweets:
                    break
            except:
                continue
        if tweets:
            break
    if not tweets:
        return 0, None
    pos = ["moon","gem","100x","buy","bullish","🔥","🚀","💎","alpha","early","send","launch"]
    neg = ["rug","scam","dump","rekt","honeypot","avoid","warning","sus","fake"]
    p   = sum(1 for t in tweets for w in pos if w in t.lower())
    n   = sum(1 for t in tweets for w in neg if w in t.lower())
    bonus     = min(len(tweets) * 2, 10)
    sentiment = "🟢 Positif" if p > n else "🔴 Negatif" if n > p else "🟡 Netral"
    if n > p:
        bonus -= 10
    return bonus, f"📱 Twitter: {len(tweets)} mention | {sentiment}"


# ══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════

def fetch_token_best_pair(addr):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
        if r.status_code != 200:
            return None, []
        sol_p = [p for p in r.json().get("pairs", [])
                 if p.get("chainId") == "solana"]
        if not sol_p:
            return None, []
        sol_p.sort(
            key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True)
        urls = [(p.get("dexId","?"), p.get("url",""),
                 float(p.get("liquidity",{}).get("usd",0) or 0))
                for p in sol_p]
        return sol_p[0], urls
    except:
        return None, []


def fetch_dead_coin_revival_candidates():
    """
    LAPISAN 3: Scan koin lama yang tiba-tiba dapat volume tinggi.
    Endpoint: DexScreener trending + gainers, filter token age >48 jam.
    Ini yang menangkap kasus Strawberita, WhiteWhale, Asteroid.
    """
    if not config.ENABLE_REVIVAL_SCAN:
        return []

    candidates = {}

    def add_candidate(addr, pair, urls):
        if not addr or addr in candidates:
            return
        liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        if liq < config.MIN_LIQUIDITY_USD:
            return
        candidates[addr] = (pair, urls)

    # Endpoint 1: DexScreener trending gainers
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/search?q=solana",
            timeout=10)
        if r.status_code == 200:
            for p in r.json().get("pairs", [])[:50]:
                if p.get("chainId") != "solana":
                    continue
                ca   = p.get("pairCreatedAt", 0)
                age_h = (time.time() - ca / 1000) / 3600 if ca else 0
                if age_h < config.REVIVAL_MIN_TOKEN_AGE_H:
                    continue
                # Hanya token tua dengan price change positif = potensi revival
                pc_1h = float((p.get("priceChange") or {}).get("h1", 0) or 0)
                v1h   = float((p.get("volume") or {}).get("h1", 0) or 0)
                if pc_1h > 10 and v1h > config.REVIVAL_MIN_VOL_1H:
                    addr = p.get("baseToken", {}).get("address", "")
                    liq  = float(p.get("liquidity", {}).get("usd", 0) or 0)
                    urls = [(p.get("dexId","?"), p.get("url",""), liq)]
                    add_candidate(addr, p, urls)
    except Exception as e:
        print(f"  [Revival] Endpoint 1 err: {e}")

    # Endpoint 2: Token dengan volume spike (search volume tinggi)
    for query in ["sol pump revival", "solana memecoin"]:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={query}",
                timeout=8)
            if r.status_code == 200:
                for p in r.json().get("pairs", [])[:30]:
                    if p.get("chainId") != "solana":
                        continue
                    ca    = p.get("pairCreatedAt", 0)
                    age_h = (time.time() - ca / 1000) / 3600 if ca else 0
                    if age_h < config.REVIVAL_MIN_TOKEN_AGE_H:
                        continue
                    v1h = float((p.get("volume") or {}).get("h1", 0) or 0)
                    v6h = float((p.get("volume") or {}).get("h6", 0) or 0)
                    # Spike ratio: v1h vs v6h average
                    avg6 = v6h / 6 if v6h > 0 else 0
                    if avg6 > 0 and v1h / avg6 > config.DEAD_COIN_VOL_MULTIPLIER:
                        addr = p.get("baseToken", {}).get("address", "")
                        liq  = float(p.get("liquidity", {}).get("usd", 0) or 0)
                        urls = [(p.get("dexId","?"), p.get("url",""), liq)]
                        add_candidate(addr, p, urls)
        except Exception as e:
            print(f"  [Revival] Endpoint 2 err: {e}")

    print(f"  [Revival] Found {len(candidates)} candidates")
    return list(candidates.values())[:config.DEAD_COIN_SCAN_LIMIT]


def get_solana_pairs():
    token_map  = {}
    seen_token = set()
    boosted_tokens.clear()

    def add(addr, pair, urls):
        if not addr or addr in seen_token:
            return
        seen_token.add(addr)
        cur_liq  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        existing = token_map.get(addr)
        if not existing:
            token_map[addr] = (pair, urls)
        else:
            ex_liq = float(existing[0].get("liquidity", {}).get("usd", 0) or 0)
            if cur_liq > ex_liq:
                token_map[addr] = (pair, urls)

    def fetch_and_add(addr):
        if not addr or addr in seen_token:
            return
        p, urls = fetch_token_best_pair(addr)
        if p:
            add(addr, p, urls)
        time.sleep(0.15)

    # Endpoint original
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        if r.status_code == 200:
            tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Profiles: {len(tokens)}")
            for t in tokens[:25]:
                fetch_and_add(t.get("tokenAddress", ""))
    except Exception as e:
        print(f"  EP1 err: {e}")

    try:
        r = requests.get(
            "https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        if r.status_code == 200:
            tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Boosted: {len(tokens)}")
            for t in tokens[:20]:
                addr = t.get("tokenAddress", "")
                if addr:
                    boosted_tokens.add(addr)
                fetch_and_add(addr)
    except Exception as e:
        print(f"  EP2 err: {e}")

    for query in ["pump sol", "raydium"]:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={query}", timeout=10)
            if r.status_code == 200:
                for p in r.json().get("pairs", []):
                    if p.get("chainId") == "solana":
                        addr = p.get("baseToken", {}).get("address", "")
                        liq  = float(p.get("liquidity",{}).get("usd",0) or 0)
                        urls = [(p.get("dexId","?"), p.get("url",""), liq)]
                        add(addr, p, urls)
        except Exception as e:
            print(f"  EP search err: {e}")

    print(f"  Unique: {len(token_map)} | Boosted: {len(boosted_tokens)}")
    return list(token_map.values())

def get_token_details(pair, all_urls=None):
    try:
        base = pair.get("baseToken", {})
        liq  = pair.get("liquidity", {})
        vol  = pair.get("volume", {})
        pc   = pair.get("priceChange", {})
        txns = pair.get("txns", {})
        ca   = pair.get("pairCreatedAt", 0)
        age_h = (time.time() - ca / 1000) / 3600 if ca else 9999
        h1  = txns.get("h1", {});  h6  = txns.get("h6", {});  h24 = txns.get("h24", {})
        h1b = h1.get("buys",0);    h1s = h1.get("sells",0)
        h6b = h6.get("buys",0);    h6s = h6.get("sells",0)
        h24b = h24.get("buys",0);  h24s = h24.get("sells",0)
        makers = (h24.get("makers") or h6.get("makers") or
                  h1.get("makers") or pair.get("makers") or 0)
        v1h  = float(vol.get("h1", 0) or 0)
        v6h  = float(vol.get("h6", 0) or 0)
        v24h = float(vol.get("h24", 0) or 0)
        avg6h     = v6h / 6 if v6h > 0 else 0
        vol_accel = round(v1h / avg6h, 2) if avg6h > 50 else 0
        wash = 0; abpm = 0
        if makers > 0 and h24b > 0:
            abpm = h24b / makers
            if abpm > 6:     wash = 3
            elif abpm > 4:   wash = 2
            elif abpm > 2.5: wash = 1
        addr = base.get("address", "")
        liq_usd = float(liq.get("usd", 0) or 0)
        return {
            "name":     base.get("name", "Unknown"),
            "symbol":   base.get("symbol", "???"),
            "address":  addr,
            "mcap":     float(pair.get("marketCap", 0) or 0),
            "liq":      liq_usd,
            "v1h": v1h, "v6h": v6h, "v24h": v24h,
            "vol_accel":  vol_accel,
            "pc_5m":  float(pc.get("m5", 0) or 0),
            "pc_1h":  float(pc.get("h1", 0) or 0),
            "pc_6h":  float(pc.get("h6", 0) or 0),
            "pc_24h": float(pc.get("h24", 0) or 0),
            "age_h":  round(age_h, 1),
            "h1_bsr":  round(h1b  / max(h1s,  1), 2),
            "h6_bsr":  round(h6b  / max(h6s,  1), 2),
            "h24_bsr": round(h24b / max(h24s, 1), 2),
            "makers":  makers,
            "wash":    wash,
            "abpm":    round(abpm, 1),
            "bc_pct":  pumpfun_bc.get(addr, 0),
            "is_boosted": addr in boosted_tokens,
            "pair_url":   pair.get("url", ""),
            "all_pair_urls": all_urls or [],
            "dex_id":  pair.get("dexId", ""),
        }
    except Exception as e:
        print(f"  Parse err: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  FILTER DASAR
# ══════════════════════════════════════════════════════════════

def passes_filter(token, is_revival=False):
    if token["mcap"] <= 0:                      return False, "MCap invalid"
    if token["liq"] < config.MIN_LIQUIDITY_USD: return False, "Liq rendah"
    if token["wash"] >= 3:                      return False, f"Wash ({token['abpm']}x/wallet)"
    if token["v1h"] < config.MIN_VOLUME_1H:     return False, f"Vol1h<${config.MIN_VOLUME_1H:,}"

    # Revival token dapat bypass filter downtrend
    # karena koin baru bangkit sering pc_6h masih negatif
    if not is_revival:
        if token["pc_1h"] < -5 and token["vol_accel"] < 1.0:
            return False, f"Distribusi:{token['pc_1h']}%"
        if token["pc_1h"] < -10 and token["pc_6h"] < -15:
            return False, f"Downtrend:1h={token['pc_1h']}%,6h={token['pc_6h']}%"

    if token["v24h"] > 0 and token["mcap"] > 0:
        if token["v24h"] / token["mcap"] > 100:
            return False, "Vol/MCap>100x"
    return True, "OK"


# ══════════════════════════════════════════════════════════════
#  LAPISAN 4 — SCORING
# ══════════════════════════════════════════════════════════════

def score_token(token, revival_data=None):
    """
    revival_data: (is_revival, confidence, notes) dari detect_revival_signal()
    """
    score    = 0
    reasons  = []
    warnings = []
    age      = token["age_h"]

    # Cek resurrection dari volume history
    is_res, res_mult = detect_resurrection(token["address"], token["v1h"])

    # Cek revival dari scanner (lapisan 3)
    is_rev, rev_conf, rev_notes = revival_data if revival_data else (False, "", [])

    # Tentukan tipe token
    if age < config.AGE_TIER_FRESH and token["v1h"] > 2000:
        ttype = "FRESH_GRADUATE"
    elif is_rev and rev_conf in ("HIGH", "MEDIUM"):
        ttype = "REVIVAL"          # v12: tipe baru untuk dead coin revival
    elif is_res:
        ttype = "RESURRECTION"
    elif token["pc_1h"] > 15 and token["h1_bsr"] > 1.5:
        ttype = "MOMENTUM"
    elif token["vol_accel"] > 2 and token["h1_bsr"] > 1.3:
        ttype = "ACCUMULATION"
    else:
        ttype = "NORMAL"

    # Age modifier
    if age < config.AGE_TIER_FRESH:       age_mod = 0
    elif age < config.AGE_TIER_YOUNG:     age_mod = -5
    elif age < config.AGE_TIER_NORMAL:    age_mod = -10
    elif age < config.AGE_TIER_OLD:       age_mod = -20
    else:
        age_mod = -config.SCORE_PENALTY_OLD_TOKEN
        warnings.append(f"⏰ Token tua ({round(age/24,1)} hari)")

    # Revival dan resurrection mengurangi age penalty secara signifikan
    if ttype in ("RESURRECTION", "REVIVAL"):
        age_mod = max(age_mod // 4, -5)

    score += age_mod

    # ── Scoring per tipe ──────────────────────────────────────

    if ttype == "FRESH_GRADUATE":
        score += 20; reasons.append(f"🆕 Fresh graduate ({round(age,1)} jam)")
        bsr = token["h1_bsr"]
        if bsr > 3:      score += 30; reasons.append(f"💚 Buy pressure kuat ({bsr}x)")
        elif bsr > 2:    score += 20; reasons.append(f"✅ Buy dominan ({bsr}x)")
        elif bsr > 1.3:  score += 10; reasons.append(f"🟡 Buy moderate ({bsr}x)")
        if token["v1h"] > 50000:     score += 20; reasons.append(f"🔥 Vol1h ${token['v1h']:,.0f}")
        elif token["v1h"] > 10000:   score += 12; reasons.append(f"📈 Vol1h ${token['v1h']:,.0f}")
        elif token["v1h"] > 3000:    score += 5
        if token["mcap"] < 100000:   score += 15; reasons.append(f"💰 MCap kecil ${token['mcap']:,.0f}")
        elif token["mcap"] < 500000: score += 8;  reasons.append(f"💰 MCap ${token['mcap']:,.0f}")

    elif ttype == "REVIVAL":
        # v12: Dead Coin Revival — weighted scoring
        rev_bonus = get_revival_score_bonus(rev_conf)
        weighted  = int(rev_bonus * config.SCORE_WEIGHT_REVIVAL)
        score += weighted
        reasons.append(f"🔄 DEAD COIN REVIVAL [{rev_conf}] (+{weighted}pts)")
        for note in rev_notes:
            if "✅" in note or "🚀" in note or "💚" in note:
                reasons.append(note)
            else:
                warnings.append(note)
        bsr = token["h1_bsr"]
        if bsr > 2:      score += 25; reasons.append(f"💚 Buy pressure ({bsr}x)")
        elif bsr > 1.3:  score += 15; reasons.append(f"🟡 Buy ada ({bsr}x)")
        if token["pc_1h"] > 50:    score += 25; reasons.append(f"🚀 +{token['pc_1h']}% 1h!")
        elif token["pc_1h"] > 20:  score += 12; reasons.append(f"📈 +{token['pc_1h']}%")
        if token["v1h"] > 20000:   score += 15; reasons.append(f"🔥 Vol1h ${token['v1h']:,.0f}")
        if token["makers"] > 0 and token["makers"] < 30:
            score -= 15; warnings.append(f"⚠️ Makers rendah ({token['makers']})")

    elif ttype == "RESURRECTION":
        score += 35; reasons.append(f"⚡ RESURRECTION {res_mult}x ({round(age/24,1)} hari)")
        bsr = token["h1_bsr"]
        if bsr > 2:      score += 25; reasons.append(f"💚 Buy pressure ({bsr}x)")
        elif bsr > 1.3:  score += 15; reasons.append(f"🟡 Buy ada ({bsr}x)")
        if token["pc_1h"] > 50:    score += 25; reasons.append(f"🚀 +{token['pc_1h']}% 1h!")
        elif token["pc_1h"] > 20:  score += 12; reasons.append(f"📈 +{token['pc_1h']}%")
        if token["v1h"] > 20000:   score += 15; reasons.append(f"🔥 Vol1h ${token['v1h']:,.0f}")
        if token["makers"] > 0 and token["makers"] < 30:
            score -= 15; warnings.append(f"⚠️ Makers rendah ({token['makers']})")

    elif ttype == "MOMENTUM":
        score += 10; reasons.append(f"🚀 Momentum +{token['pc_1h']}%")
        bsr = token["h1_bsr"]
        if bsr > 3:   score += 25; reasons.append(f"💚 Buy pressure ({bsr}x)")
        elif bsr > 2: score += 18; reasons.append(f"✅ Buy dominan ({bsr}x)")
        va = token["vol_accel"]
        if va > 4:    score += 20; reasons.append(f"🔥 Vol accel {va}x")
        elif va > 2:  score += 12; reasons.append(f"📊 Vol accel {va}x")

    else:
        va = token["vol_accel"]
        if va > 3:      score += 20; reasons.append(f"📊 Vol accel {va}x")
        elif va > 2:    score += 12; reasons.append(f"📊 Vol accel {va}x")
        bsr = token["h1_bsr"]
        if bsr > 2.5:   score += 20; reasons.append(f"💚 Buy dominan ({bsr}x)")
        elif bsr > 1.5: score += 12; reasons.append(f"🟡 Buy dominan ({bsr}x)")
        elif bsr < 0.7: score -= 20; warnings.append("🔴 Sell pressure tinggi!")

    # Liquidity ratio
    if token["mcap"] > 0:
        lr = token["liq"] / token["mcap"]
        if lr > 0.15:   score += 10; reasons.append(f"💧 Liq sehat ({round(lr*100,1)}%)")
        elif lr > 0.05: score += 5
        elif lr < 0.02: score -= 10; warnings.append("⚠️ Liq tipis!")

    # BC pump.fun
    bc = token.get("bc_pct", 0)
    if bc >= 90:    score += 20; reasons.append(f"🔥 BC {bc}% — hampir graduate!")
    elif bc >= 75:  score += 10; reasons.append(f"📈 BC {bc}%")

    # Penalty boosted
    if token["is_boosted"]:
        score -= config.SCORE_PENALTY_BOOSTED
        warnings.append("⚠️ Boosted (dev promotion)")

    # Penalty wash
    if token["wash"] == 2:
        score -= config.SCORE_PENALTY_WASH2
        warnings.append(f"🔶 Wash ({token['abpm']}x/wallet)")
    elif token["wash"] == 1:
        score -= 5
        warnings.append(f"⚠️ Suspicious ({token['abpm']}x/wallet)")

    if token["pc_1h"] < -25: score -= 20; warnings.append(f"📉 Dump -{abs(token['pc_1h'])}% 1h!")
    if token["pc_5m"] > 5:   score += 5;  reasons.append(f"🟢 +{token['pc_5m']}% 5m")

    return score, reasons, warnings, ttype

def get_signal(score, ttype):
    if score >= config.SCORE_MOONBAG:
        d = "MCap kecil + momentum = potensi 100x+" if ttype in ["FRESH_GRADUATE","RESURRECTION","REVIVAL"] \
            else "Hold berminggu-minggu"
        return "🌙 MOONBAG CANDIDATE", d
    elif score >= config.SCORE_SWING:
        return "🎯 SWING TARGET", "Hold 1-7 hari"
    elif score >= config.SCORE_SCALP:
        return {
            "FRESH_GRADUATE": ("⚡ SCALP — FRESH GRADUATE", "Tangkap momentum awal"),
            "RESURRECTION":   ("⚡ SCALP — RESURRECTION",   "Volume bangkit, wave pertama"),
            "REVIVAL":        ("⚡ SCALP — REVIVAL",         "Dead coin hidup, entry cepat"),
            "MOMENTUM":       ("⚡ SCALP — MOMENTUM",        "Momentum kuat, exit cepat"),
        }.get(ttype, ("⚡ SCALP", "Hold <2 jam"))
    return None, None


# ══════════════════════════════════════════════════════════════
#  EXIT SIGNAL
# ══════════════════════════════════════════════════════════════

def detect_exit_signal(token):
    urgency = 0; reasons = []
    addr = token["address"]
    if token["h1_bsr"] < config.EXIT_BSR_DANGER:
        urgency = max(urgency, 2)
        reasons.append(f"🔴 BSR 1h: {token['h1_bsr']} — distribusi berat!")
    if token["pc_1h"] < config.EXIT_DUMP_PCT_1H:
        urgency = max(urgency, 3)
        reasons.append(f"🔴 Dump {token['pc_1h']}% dalam 1 jam!")
    prev = hold_vol_prev.get(addr, token["vol_accel"])
    if prev > 1 and token["vol_accel"] < prev * config.EXIT_VOL_COLLAPSE:
        urgency = max(urgency, 2)
        reasons.append(f"📉 Vol collapse: {prev}x → {token['vol_accel']}x")
    hold_vol_prev[addr] = token["vol_accel"]
    if token["wash"] >= 2:
        urgency = max(urgency, 1)
        reasons.append("🔶 Wash trading muncul!")
    if token["mcap"] > 0 and token["liq"] / token["mcap"] < 0.02:
        urgency = max(urgency, 2)
        reasons.append("⚠️ Liquidity sangat tipis!")
    # v12: LP trend menurun = exit signal tambahan
    liq_trend = get_liq_trend(addr)
    if liq_trend == "decreasing":
        urgency = max(urgency, 2)
        reasons.append("🔴 LP sedang ditarik — risiko rug!")
    return urgency, reasons


# ══════════════════════════════════════════════════════════════
#  AUTO WATCHLIST CHECKER
# ══════════════════════════════════════════════════════════════

def aw_mcap_change(mcap0, mcap_now):
    if mcap0 <= 0:
        return "N/A", "", 0
    pct = ((mcap_now - mcap0) / mcap0) * 100
    if pct >= 200:   emoji = "🚀🚀🚀"
    elif pct >= 100: emoji = "🚀🚀"
    elif pct >= 50:  emoji = "🚀"
    elif pct >= 20:  emoji = "📈"
    elif pct >= 0:   emoji = "🟢"
    elif pct >= -20: emoji = "🟡"
    elif pct >= -50: emoji = "🔴"
    else:            emoji = "💀"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{round(pct,1)}%", emoji, pct

def aw_vol_change(v0, v_now):
    if v0 <= 0: return "N/A"
    ratio = v_now / v0
    if ratio >= 2:     return f"🔥 {round(ratio,1)}x naik"
    elif ratio >= 1.1: return f"📈 {round(ratio,1)}x naik"
    elif ratio >= 0.8: return f"🟡 Stabil"
    elif ratio >= 0.5: return f"📉 Turun {round((1-ratio)*100):.0f}%"
    else:              return f"💀 Drop {round((1-ratio)*100):.0f}%"

async def check_auto_watchlist(bot, silent=False):
    if not auto_watchlist:
        return
    print(f"\n  [AW] Checking {len(auto_watchlist)} tokens (silent={silent})...")
    now = time.time()

    for addr, aw in list(auto_watchlist.items()):
        try:
            pair, urls = fetch_token_best_pair(addr)
            if not pair:
                continue
            t = get_token_details(pair, urls)
            if not t:
                continue

            track_volume(addr, t["v1h"])
            track_liq(addr, t["liq"])
            score_now, _, warnings, ttype = score_token(t)
            eu, er = detect_exit_signal(t)

            auto_watchlist[addr]["mcap_now"]  = t["mcap"]
            auto_watchlist[addr]["v1h_now"]   = t["v1h"]
            auto_watchlist[addr]["score_now"] = score_now
            auto_watchlist[addr]["check_ts"]  = now

            mcap0     = aw.get("mcap0", 0)
            v1h0      = aw.get("v1h0", 0)
            score0    = aw.get("score0", 0)
            held_h    = round((now - aw.get("ts", now)) / 3600, 1)
            name      = aw.get("name", "?")
            symbol    = aw.get("symbol", "?")
            sig0      = aw.get("sig", "?")
            pair_url  = t["pair_url"] or aw.get("pair_url", "")

            mc_pct_str, mc_emoji, mc_pct_raw = aw_mcap_change(mcap0, t["mcap"])
            vol_str = aw_vol_change(v1h0, t["v1h"])

            status, status_ico, status_text = get_aw_status(score_now, eu)
            auto_watchlist[addr]["status"] = status

            prev_pump_lvl = aw_pump_alerted.get(addr, 0)
            pump_alert    = False
            pump_note     = ""
            if mc_pct_raw >= config.AW_MOON_ALERT_PCT and prev_pump_lvl < config.AW_MOON_ALERT_PCT:
                aw_pump_alerted[addr] = config.AW_MOON_ALERT_PCT
                pump_alert = True
                pump_note  = f"🚀🚀🚀 *{mc_pct_str} dari entry! Pertimbangkan take profit sebagian*"
            elif mc_pct_raw >= config.AW_PUMP_ALERT_PCT and prev_pump_lvl < config.AW_PUMP_ALERT_PCT:
                aw_pump_alerted[addr] = config.AW_PUMP_ALERT_PCT
                pump_alert = True
                pump_note  = f"🚀 *{mc_pct_str} dari entry! Pertimbangkan take profit sebagian*"

            is_res, res_mult = detect_resurrection(addr, t["v1h"])
            res_note = ""
            if is_res and ttype == "RESURRECTION":
                res_note = f"\n⚡ *RESURRECTION TERDETEKSI!* Vol {res_mult}x dari sebelumnya\n"
                if silent:
                    pump_alert = True

            # v12: LP drain alert di AW
            liq_trend = get_liq_trend(addr)
            liq_note  = ""
            if liq_trend == "decreasing" and silent:
                liq_note   = "\n🔴 *LP SEDANG DITARIK — Pantau exit!*\n"
                pump_alert = True  # paksa kirim

            should_send = (
                not silent
                or eu >= 2
                or pump_alert
                or status in ("EXIT", "EXIT_WARN")
            )
            if not should_send:
                continue

            msg = (
                f"{status_ico} *AUTO WATCHLIST UPDATE*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{name}* (${symbol})\n"
                f"CA: `{addr}`\n"
                f"Sinyal awal: _{sig0}_ | Hold: {held_h} jam\n"
                f"{res_note}{liq_note}\n"
                f"📊 *Performa dari entry:*\n"
                f"├ MCap entry: ${mcap0:,.0f}\n"
                f"├ MCap kini:  ${t['mcap']:,.0f} {mc_emoji} {mc_pct_str}\n"
                f"├ Vol1h entry: ${v1h0:,.0f}\n"
                f"├ Vol1h kini:  ${t['v1h']:,.0f} — {vol_str}\n"
                f"├ Score entry: {score0} | Score kini: {score_now}\n"
                f"├ BSR 1h: {t['h1_bsr']}x\n"
                f"└ 5m:{t['pc_5m']}% | 1h:{t['pc_1h']}% | 6h:{t['pc_6h']}%\n"
            )
            if pump_note:
                msg += f"\n{pump_note}\n"
            if status == "EXIT":
                msg += f"\n🔴 *{status_text}*\n"
                if er: msg += "\n".join(er)
            elif status == "EXIT_WARN":
                msg += f"\n🔶 *{status_text}*\n"
                if er: msg += "\n".join(er)
            elif status == "WEAK":
                msg += f"\n🟡 *{status_text}*\n"
                if warnings: msg += "\n".join(warnings[:2])
            else:
                msg += f"\n🟢 *{status_text}*\n"

            msg += f"\n🔗 [Chart]({pair_url})"
            msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{addr})"
            msg += f"\n_/aw remove {addr[:12]}..._"
            msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID, text=msg,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            await asyncio.sleep(1.5)

        except Exception as e:
            print(f"  [AW] err {addr[:8]}: {e}")

    cleanup_auto_watchlist()
    save_state()
    print(f"  [AW] Done. {len(auto_watchlist)} tracked.")


# ══════════════════════════════════════════════════════════════
#  ALERT FORMAT
# ══════════════════════════════════════════════════════════════

def fmt_age(h):
    if h < 24:    return f"{round(h,1)} jam"
    elif h < 720: return f"{round(h/24,1)} hari"
    else:         return f"{round(h/24/30,1)} bulan"

async def send_alert(bot, token, score, sig, desc,
                     reasons, warnings, ttype,
                     tw=None, gmgn_summary=None,
                     insider_wallets=None,
                     holder_warning=None, holder_pct=0,
                     rug_score=0, revival_notes=None):

    wash_l = ["✅ Organik","🟡 Suspicious","🔶 High Sus","🔴 Wash"][min(token["wash"],3)]
    te     = {
        "FRESH_GRADUATE":"🆕","RESURRECTION":"⚡",
        "REVIVAL":"🔄","MOMENTUM":"🚀","ACCUMULATION":"📈"
    }.get(ttype,"📊")
    boost  = "\n_⚠️ Boosted (dev promotion)_" if token["is_boosted"] else ""
    sm_sec = f"\n🧠 *SM:* {gmgn_summary}\n" if gmgn_summary else ""

    ins_sec = ""
    if insider_wallets:
        lines = [f"  • `{w['wallet'][:6]}...{w['wallet'][-4:]}` [{w['label']}] "
                 f"{'+'if w['pnl']>=0 else''}${abs(w['pnl']):,.0f}"
                 for w in insider_wallets[:3]]
        ins_sec = "\n💼 *Insiders:*\n" + "\n".join(lines) + "\n"

    pair_sec = ""
    if len(token["all_pair_urls"]) > 1:
        lines = [f"{'★'if i==0 else' '} [{d}]({u}) Liq:${l:,.0f}"
                 for i,(d,u,l) in enumerate(token["all_pair_urls"][:3])]
        pair_sec = "\n🔀 *Pairs:*\n" + "\n".join(lines) + "\n"

    holder_sec = ""
    if holder_warning:
        holder_sec = f"\n{holder_warning}\n"
    elif holder_pct > 0:
        holder_sec = f"\n✅ Holder top10={holder_pct}%\n"

    bc_note = f"\n⛽ BC: {token['bc_pct']}%\n" if token.get("bc_pct", 0) > 0 else ""

    # v12: Rug score indicator
    rug_sec = ""
    if rug_score > 0:
        if rug_score >= config.RUG_SCORE_WARN:
            rug_sec = f"\n⚠️ *Rug Score: {rug_score}/100* — Trade dengan hati-hati!\n"
        else:
            rug_sec = f"\n🛡 Rug Score: {rug_score}/100 (rendah)\n"

    # v12: Revival notes
    revival_sec = ""
    if revival_notes and ttype == "REVIVAL":
        revival_sec = "\n🔄 *Revival Analysis:*\n" + "\n".join(revival_notes[:5]) + "\n"

    msg = (
        f"{sig}\n━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token['name']}* (${token['symbol']}){boost}\n"
        f"{te} *{ttype}* | 📊 Score: *{score}/100*\n"
        f"💡 {desc}\n{sm_sec}"
        f"\nCA: `{token['address']}`\n"
        f"DEX: {token['dex_id']} | Age: {fmt_age(token['age_h'])}\n"
        f"{bc_note}"
        f"\n📈 *Market Data*\n"
        f"├ MCap: ${token['mcap']:,.0f}\n"
        f"├ Liq: ${token['liq']:,.0f}\n"
        f"├ Vol 1h: ${token['v1h']:,.0f}\n"
        f"├ Vol 24h: ${token['v24h']:,.0f}\n"
        f"├ BSR 1h: {token['h1_bsr']}x\n"
        f"├ Vol Accel: {token['vol_accel']}x\n"
        f"├ Trading: {wash_l}\n"
        f"└ 5m:{token['pc_5m']}% 1h:{token['pc_1h']}% 6h:{token['pc_6h']}%\n"
        f"{rug_sec}{revival_sec}{holder_sec}{pair_sec}{ins_sec}"
        f"\n✅ *Kenapa Menarik:*\n" + "\n".join(reasons)
    )
    if warnings:
        msg += "\n\n⚠️ *Warning:*\n" + "\n".join(warnings)
    if tw:
        msg += f"\n\n{tw}"
    msg += f"\n\n🔗 [Chart]({token['pair_url']})"
    msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{token['address']})"
    msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

    await bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID, text=msg,
        parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


# ══════════════════════════════════════════════════════════════
#  HOLD WATCHLIST
# ══════════════════════════════════════════════════════════════

async def check_hold_watchlist(bot):
    if not hold_watchlist:
        return
    for addr in list(hold_watchlist):
        try:
            pair, urls = fetch_token_best_pair(addr)
            if not pair:
                continue
            t = get_token_details(pair, urls)
            if not t:
                continue
            track_volume(addr, t["v1h"])
            track_liq(addr, t["liq"])
            score, _, warnings, _ = score_token(t)
            eu, er = detect_exit_signal(t)
            gb, gs, _, _ = gmgn_check_smart_money(addr, t["name"])
            score += gb
            status, ue, ut = get_aw_status(score, eu)
            liq_trend = get_liq_trend(addr)
            liq_warn  = "🔴 LP ditarik!" if liq_trend == "decreasing" else ""
            msg = (
                f"📋 *HOLD MONITOR*\n━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{t['name']}* (${t['symbol']})\n"
                f"{ue} Score: *{score}/100* | {ut}\n"
                f"CA: `{addr}`\n\n"
                f"5m:{t['pc_5m']}% | 1h:{t['pc_1h']}% | 6h:{t['pc_6h']}%\n"
                f"├ Vol 1h: ${t['v1h']:,.0f} | Accel: {t['vol_accel']}x\n"
                f"├ BSR 1h: {t['h1_bsr']}x\n"
                f"└ Liq: ${t['liq']:,.0f} {''+liq_warn}\n"
            )
            if gs:
                msg += f"\n🧠 GMGN: {gs}\n"
            if er:
                msg += "\n🚨 *Exit Signals:*\n" + "\n".join(er)
            if warnings:
                msg += "\n⚠️ " + " | ".join(warnings)
            msg += f"\n\n🔗 [Chart]({t['pair_url']}) | [GMGN](https://gmgn.ai/sol/token/{addr})"
            msg += f"\n_/hold remove {addr}_"
            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID, text=msg,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  Hold err: {e}")


# ══════════════════════════════════════════════════════════════
#  HELIUS WALLET POLLER
# ══════════════════════════════════════════════════════════════

def get_wallet_recent_buys(wallet, limit=5):
    if not config.HELIUS_API_KEY:
        return []
    try:
        r = requests.get(
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
            params={"type":"SWAP","limit":limit,"api-key":config.HELIUS_API_KEY},
            timeout=8)
        if r.status_code != 200:
            return []
        buys = []
        for tx in r.json():
            if time.time() - tx.get("timestamp",0) > 1800:
                continue
            for t in tx.get("events",{}).get("swap",{}).get("tokenOutputs",[]):
                mint = t.get("mint","")
                if mint and mint != "So11111111111111111111111111111111111111112":
                    buys.append({"address":mint,"wallet":wallet,
                                 "timestamp":tx.get("timestamp",0)})
        return buys
    except Exception as e:
        print(f"  Helius err: {e}")
        return []

async def poll_smart_money_wallets(bot):
    if not sm_wallets or not config.HELIUS_API_KEY:
        return
    for wallet in sm_wallets:
        try:
            for buy in get_wallet_recent_buys(wallet):
                addr = buy["address"]
                if sm_last_buy.get(wallet,{}).get("token") == addr:
                    continue
                sm_last_buy[wallet] = {"token":addr,"time":buy["timestamp"]}
                pair, urls = fetch_token_best_pair(addr)
                if not pair:
                    continue
                t = get_token_details(pair, urls)
                if not t:
                    continue
                score, _, warnings, ttype = score_token(t)
                gb, gs, _, _ = gmgn_check_smart_money(addr, t["name"])
                score += gb
                ws = wallet[:6]+"..."+wallet[-4:]
                ts = datetime.fromtimestamp(buy["timestamp"]).strftime('%H:%M:%S')
                msg = (
                    f"🧠 *SMART MONEY ALERT*\n━━━━━━━━━━━━━━━━━━━\n"
                    f"`{ws}` beli! ({ts} WIB)\n\n"
                    f"🪙 *{t['name']}* (${t['symbol']})\n"
                    f"CA: `{t['address']}`\n"
                    f"Score: *{score}/100*\n\n"
                    f"MCap: ${t['mcap']:,.0f} | Age: {fmt_age(t['age_h'])}\n"
                    f"Vol 1h: ${t['v1h']:,.0f} | BSR: {t['h1_bsr']}x\n"
                )
                if gs: msg += f"\n🧠 GMGN: {gs}"
                msg += f"\n\n🔗 [Chart]({t['pair_url']}) | [GMGN](https://gmgn.ai/sol/token/{addr})"
                msg += f"\n_/hold {addr}_"
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID, text=msg,
                    parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                await asyncio.sleep(1.5)
        except Exception as e:
            print(f"  SM poll err: {e}")
    save_state()


# ══════════════════════════════════════════════════════════════
#  MAIN SCAN — v12 dengan Revival Scan terpisah
# ══════════════════════════════════════════════════════════════

async def process_token(bot, t, is_revival_scan=False, revival_data=None):
    """
    Proses satu token: filter → rug check → score → alert.
    Return True kalau alert dikirim.
    """
    addr = t["address"]

    track_volume(addr, t["v1h"])
    track_liq(addr, t["liq"])

    if addr in auto_watchlist:
        return False
    if is_seen(addr):
        return False

    ok, reason = passes_filter(t, is_revival=is_revival_scan)
    if not ok:
        print(f"  x {t['name'][:20]:<20} {reason}")
        return False

    # GMGN — dapat traders untuk rug check
    gb, gs, gi, raw_traders = 0, None, [], []
    if config.ENABLE_GMGN_SMART_MONEY:
        gb, gs, gi, raw_traders = gmgn_check_smart_money(addr, t["name"])

    # LAPISAN 1: Holder check + Rug filter
    hok, hpct, h3pct, hwarn = check_holder_distribution(addr)
    rug_passed, rug_reason, rug_score = check_rug_filter(
        t, hok, h3pct, hpct, raw_traders)

    if not rug_passed:
        print(f"  ✗ RUG [{t['name'][:16]}] {rug_reason}")
        return False

    # LAPISAN 3: Revival signal
    if revival_data is None:
        revival_data = detect_revival_signal(t)
    is_rev, rev_conf, rev_notes = revival_data

    score, reasons, warnings, ttype = score_token(t, revival_data=revival_data)

    # Tambah GMGN bonus setelah scoring dasar
    if gb > 0:
        score += gb
        reasons.append("🧠 Smart Money GMGN detected!")

    # Holder penalty kalau tidak oke
    if not hok:
        warnings.append(hwarn)
        score -= config.SCORE_PENALTY_HOLDER_CONC

    # Rug score warning
    if rug_score >= config.RUG_SCORE_WARN:
        warnings.append(f"⚠️ Rug Score {rug_score}/100 — hati-hati!")
        score -= 10

    sig, desc = get_signal(score, ttype)
    if not sig:
        return False

    # Twitter check kalau score cukup
    tb, ts_tw = 0, None
    if score >= 55:
        tb, ts_tw = analyze_twitter(t["name"], t["symbol"], addr)
        score += tb

    mark_seen(addr)

    if score >= config.AUTO_WL_SCORE_MIN:
        add_to_auto_watchlist(t, score, sig, ttype)

    btag = " [B]" if t["is_boosted"] else ""
    rtag = " [REV]" if is_rev else ""
    print(f"  {ttype[:5]} | {t['name'][:14]:<14} | S:{score:>3} | "
          f"MC:${t['mcap']:>9,.0f} | V1h:${t['v1h']:>7,.0f} | "
          f"BSR:{t['h1_bsr']}{btag}{rtag}")

    await send_alert(
        bot, t, score, sig, desc, reasons, warnings, ttype,
        ts_tw, gs, gi, hwarn, hpct,
        rug_score=rug_score,
        revival_notes=rev_notes if is_rev else None)
    return True


async def do_scan(bot, manual=False):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{ts}] {'Manual' if manual else 'Auto'} scan")
    if manual:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=f"🔄 *Manual Scan v12* ({ts} WIB)\n⏳ Scanning...",
            parse_mode=ParseMode.MARKDOWN)

    sent = filtered = rug_blocked = already = aw_added = 0

    # ── Scan biasa (token baru/trending) ──────────────────────
    pairs = get_solana_pairs()
    for best_pair, all_urls in pairs:
        t = get_token_details(best_pair, all_urls)
        if not t or not t["address"]:
            continue
        sent_this = await process_token(bot, t)
        if sent_this:
            sent += 1
            await asyncio.sleep(1.5)

    # ── LAPISAN 3: Dead Coin Revival Scan ────────────────────
    if config.ENABLE_REVIVAL_SCAN:
        print(f"\n  [Revival Scan] Starting...")
        revival_pairs = fetch_dead_coin_revival_candidates()
        rev_sent = 0
        for best_pair, all_urls in revival_pairs:
            t = get_token_details(best_pair, all_urls)
            if not t or not t["address"]:
                continue
            # Pre-compute revival data
            revival_data = detect_revival_signal(t)
            is_rev, rev_conf, _ = revival_data
            if not is_rev:
                continue  # Skip kalau tidak terdeteksi sebagai revival
            sent_this = await process_token(
                bot, t, is_revival_scan=True, revival_data=revival_data)
            if sent_this:
                rev_sent += 1
                await asyncio.sleep(1.5)
        print(f"  [Revival Scan] {rev_sent} alerts dari {len(revival_pairs)} candidates")
        sent += rev_sent

    await check_hold_watchlist(bot)

    if manual:
        aw_hold = sum(1 for d in auto_watchlist.values() if d.get("status")=="HOLD")
        aw_weak = sum(1 for d in auto_watchlist.values() if d.get("status")=="WEAK")
        aw_exit = sum(1 for d in auto_watchlist.values()
                     if d.get("status") in ("EXIT","EXIT_WARN"))
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"✅ *Scan Selesai v12*\n━━━━━━━━━━━━━━━━━━━\n"
                f"📊 {len(pairs)} token biasa | 🔄 Revival scan aktif\n"
                f"🛡 Rug filter: AKTIF\n"
                f"🔔 {sent} alerts\n"
                f"📋 Auto WL: {len(auto_watchlist)} "
                f"(🟢{aw_hold} 🟡{aw_weak} 🔴{aw_exit})\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
                + ("\n\n💤 Tidak ada koin memenuhi kriteria." if sent==0 else "")
            ),
            parse_mode=ParseMode.MARKDOWN)

    cleanup_seen()
    if len(volume_history) > 3000:
        oldest = sorted(volume_history,
                        key=lambda k: volume_history[k][-1][0])[:1000]
        for k in oldest:
            del volume_history[k]
    save_state()


# ══════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v12*\n━━━━━━━━━━━━━━━━━━━\n"
        "*/scan* — Scan manual\n"
        "*/aw* — Lihat Auto Watchlist\n"
        "*/aw check* — Cek semua token di AW\n"
        "*/aw remove <CA>* — Hapus dari AW\n"
        "*/aw clear* — Bersihkan AW\n"
        "*/hold <CA>* — Tambah hold manual\n"
        "*/hold list | remove | check*\n"
        "*/wallet add | list | check*\n"
        "*/gmgn <CA>* — Cek smart money\n"
        "*/holders <CA>* — Distribusi holder\n"
        "*/revival <CA>* — Cek revival signal\n"
        "*/rug <CA>* — Cek rug score\n"
        "*/status* — Status bot\n"
        "*/clearcache* — Reset seen cache\n",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await do_scan(context.bot, manual=True)

async def cmd_aw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []

    if args and args[0] == "check":
        if not auto_watchlist:
            await update.message.reply_text("📋 Auto WL kosong.")
            return
        await update.message.reply_text(f"🔄 Cek {len(auto_watchlist)} token...")
        await check_auto_watchlist(context.bot, silent=False)
        return

    if args and args[0] == "remove" and len(args) > 1:
        target  = args[1].strip()
        removed = []
        for addr in list(auto_watchlist.keys()):
            if addr == target or addr.startswith(target):
                del auto_watchlist[addr]
                aw_pump_alerted.pop(addr, None)
                removed.append(addr[:12]+"...")
        if removed:
            save_state()
            await update.message.reply_text("✅ Dihapus:\n"+"\n".join(removed))
        else:
            await update.message.reply_text("❌ Tidak ditemukan.")
        return

    if args and args[0] == "clear":
        n = len(auto_watchlist)
        auto_watchlist.clear()
        aw_pump_alerted.clear()
        save_state()
        await update.message.reply_text(f"✅ AW dibersihkan ({n} token).")
        return

    if not auto_watchlist:
        await update.message.reply_text(
            "📋 Auto WL kosong.\nToken score bagus otomatis masuk saat scan.")
        return

    now   = time.time()
    lines = []
    for addr, d in sorted(auto_watchlist.items(),
                          key=lambda x: x[1].get("score_now",0), reverse=True):
        held_h    = round((now - d.get("ts",now)) / 3600, 1)
        mcap0     = d.get("mcap0", 0)
        mcap_now  = d.get("mcap_now", mcap0)
        pct_str, emoji, _ = aw_mcap_change(mcap0, mcap_now)
        status    = d.get("status", "HOLD")
        sico      = {"HOLD":"🟢","WEAK":"🟡","EXIT":"🔴","EXIT_WARN":"🔶","EXITED":"⚫"}.get(status,"❓")
        sn        = d.get("score_now", d.get("score0", 0))
        ttype     = d.get("ttype", "")
        ttag      = " 🔄" if ttype == "REVIVAL" else " ⚡" if ttype == "RESURRECTION" else ""
        lines.append(
            f"{sico} *{d.get('name','?')}* (${d.get('symbol','?')}){ttag}\n"
            f"  MC: ${mcap_now:,.0f} {emoji}{pct_str} | S:{sn} | {held_h}j\n"
            f"  `{addr[:20]}...`"
        )

    header = (
        f"👁 *AUTO WATCHLIST ({len(auto_watchlist)} token)*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 Hold | 🟡 Lemah | 🔴 Exit | 🔄 Revival | ⚡ Resurrection\n\n"
    )
    chunk = header
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(
                chunk, parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n\n"
    if chunk:
        chunk += "_/aw check — update sekarang_"
        await update.message.reply_text(
            chunk, parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True)

async def cmd_hold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args or args[0] == "list":
        if not hold_watchlist:
            await update.message.reply_text("📋 Hold manual kosong.")
        else:
            lines = "\n".join(f"• `{a[:8]}...{a[-4:]}`" for a in hold_watchlist)
            await update.message.reply_text(
                f"📋 *Hold manual ({len(hold_watchlist)}):*\n{lines}",
                parse_mode=ParseMode.MARKDOWN)
        return
    if args[0] == "remove" and len(args) > 1:
        hold_watchlist.discard(args[1]); save_state()
        await update.message.reply_text("✅ Dihapus.")
        return
    if args[0] == "check":
        await update.message.reply_text("🔄 Cek exit signals...")
        await check_hold_watchlist(context.bot)
        return
    addr = args[0].strip()
    if len(addr) < 20:
        await update.message.reply_text("❌ CA tidak valid.")
        return
    pair, urls = fetch_token_best_pair(addr)
    if not pair:
        await update.message.reply_text("❌ Tidak ditemukan.")
        return
    t = get_token_details(pair, urls)
    hold_watchlist.add(addr); save_state()
    await update.message.reply_text(
        f"✅ *Hold ditambahkan!*\n"
        f"🪙 {t['name']} (${t['symbol']})\nCA: `{addr}`\n"
        f"MCap: ${t['mcap']:,.0f}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args or args[0] == "list":
        hs = "✅" if config.HELIUS_API_KEY else "❌ belum diset"
        gmgn_s = "✅ aktif (tanpa Helius)" if config.ENABLE_GMGN_SMART_MONEY else "❌"
        lines  = "\n".join(f"{i+1}. `{w[:12]}...`" for i,w in enumerate(sm_wallets)) or "—"
        await update.message.reply_text(
            f"🧠 *Smart Money Config*\n━━━━━━━━━━━━━━━━━━━\n"
            f"GMGN auto SM: {gmgn_s}\n"
            f"_(cek insider per token, tanpa API key)_\n\n"
            f"Helius API: {hs}\n"
            f"_(untuk real-time wallet polling)_\n\n"
            f"Manual SM Wallets ({len(sm_wallets)}):\n{lines}\n\n"
            f"_/wallet add <addr> — tambah wallet untuk polling_",
            parse_mode=ParseMode.MARKDOWN)
        return
    if args[0] == "add" and len(args) > 1:
        w = args[1].strip()
        if w not in sm_wallets:
            sm_wallets.append(w); save_state()
        await update.message.reply_text(
            f"✅ SM Wallet ditambahkan! Total: {len(sm_wallets)}\n\n"
            f"{'⚠️ Catatan: Helius API belum diset.' if not config.HELIUS_API_KEY else '✅ Helius aktif.'}")
        return
    if args[0] == "remove" and len(args) > 1:
        w = args[1].strip()
        if w in sm_wallets:
            sm_wallets.remove(w); save_state()
        await update.message.reply_text("✅ Dihapus.")
        return
    if args[0] == "check":
        if not config.HELIUS_API_KEY:
            await update.message.reply_text(
                "❌ *Helius API belum diset*\n\n"
                "GMGN auto SM sudah aktif untuk setiap scan.\n\n"
                "Untuk real-time wallet polling:\n"
                "1. Daftar gratis di https://dev.helius.xyz/\n"
                "2. Set: `HELIUS_API_KEY=your_key`",
                parse_mode=ParseMode.MARKDOWN)
            return
        await update.message.reply_text(f"🔄 Polling {len(sm_wallets)} wallets...")
        await poll_smart_money_wallets(context.bot)

async def cmd_gmgn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /gmgn <CA>"); return
    addr    = args[0].strip()
    traders = gmgn_get_top_traders(addr, limit=10)
    if not traders:
        await update.message.reply_text("❌ Tidak ada data GMGN."); return
    bonus, summary, _, _ = gmgn_check_smart_money(addr)
    msg = f"🧠 *GMGN Report*\nBonus: +{bonus}pts\n"
    if summary: msg += f"{summary}\n"
    msg += f"\n*Top Traders:*\n"
    for i,tr in enumerate(traders[:8]):
        w  = tr.get("address",""); sh = w[:6]+"..."+w[-4:] if w else "?"
        p  = tr.get("realized_profit",0) or 0
        tg = ", ".join(str(t) for t in (tr.get("tags") or [])[:3]) or "—"
        msg += f"{i+1}. `{sh}` | {'+'if p>=0 else''}${abs(p):,.0f} | _{tg}_\n"
    msg += f"\n🔗 [GMGN](https://gmgn.ai/sol/token/{addr})"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_holders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /holders <CA>"); return
    await update.message.reply_text("⏳ Mengecek...")
    ok, pct, p3, warn = check_holder_distribution(args[0].strip())
    status = "✅ Sehat" if ok else "🔴 Terkonsentrasi"
    await update.message.reply_text(
        f"👥 *Holder Distribution*\nCA: `{args[0].strip()}`\n"
        f"Top 3:  *{p3}%* supply\n"
        f"Top 10: *{pct}%* supply | {status}\n{warn or ''}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_revival(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v12: Command baru — cek revival signal untuk CA tertentu."""
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /revival <CA>"); return
    await update.message.reply_text("⏳ Menganalisis revival signal...")
    addr = args[0].strip()
    pair, urls = fetch_token_best_pair(addr)
    if not pair:
        await update.message.reply_text("❌ Token tidak ditemukan."); return
    t = get_token_details(pair, urls)
    if not t:
        await update.message.reply_text("❌ Gagal ambil data."); return
    track_volume(addr, t["v1h"])
    track_liq(addr, t["liq"])
    is_rev, conf, notes = detect_revival_signal(t)
    is_res, res_mult = detect_resurrection(addr, t["v1h"])
    liq_trend = get_liq_trend(addr)
    liq_map = {"increasing":"📈 Bertambah","decreasing":"🔴 Berkurang","stable":"🟡 Stabil","unknown":"❓ Belum ada data"}
    msg = (
        f"🔄 *Revival Analysis*\n━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{t['name']}* (${t['symbol']})\n"
        f"CA: `{addr}`\n"
        f"Age: {fmt_age(t['age_h'])}\n\n"
        f"*Revival Signal:* {'✅ TERDETEKSI' if is_rev else '❌ Tidak terdeteksi'}\n"
        f"*Confidence:* {conf if is_rev else 'N/A'}\n"
        f"*Resurrection (history):* {'✅ '+str(res_mult)+'x' if is_res else '❌'}\n"
        f"*LP Trend:* {liq_map.get(liq_trend, '?')}\n\n"
        f"*Market:*\n"
        f"├ MCap: ${t['mcap']:,.0f} | Age: {fmt_age(t['age_h'])}\n"
        f"├ Vol 1h: ${t['v1h']:,.0f} | Vol 24h: ${t['v24h']:,.0f}\n"
        f"├ BSR 1h: {t['h1_bsr']}x\n"
        f"└ 1h: {t['pc_1h']}% | 6h: {t['pc_6h']}% | 24h: {t['pc_24h']}%\n"
    )
    if notes:
        msg += "\n*Analisis:*\n" + "\n".join(notes)
    msg += f"\n\n🔗 [Chart]({t['pair_url']})"
    msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{addr})"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_rug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v12: Command baru — cek rug score untuk CA tertentu."""
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /rug <CA>"); return
    await update.message.reply_text("⏳ Menganalisis rug risk...")
    addr = args[0].strip()
    pair, urls = fetch_token_best_pair(addr)
    if not pair:
        await update.message.reply_text("❌ Token tidak ditemukan."); return
    t = get_token_details(pair, urls)
    if not t:
        await update.message.reply_text("❌ Gagal ambil data."); return
    track_liq(addr, t["liq"])
    _, _, raw_traders = gmgn_get_top_traders(addr, limit=20), None, []
    traders = gmgn_get_top_traders(addr)
    ok, p10, p3, hwarn = check_holder_distribution(addr)
    rug_passed, rug_reason, rug_score = check_rug_filter(t, ok, p3, p10, traders)
    liq_trend = get_liq_trend(addr)

    if rug_score >= config.RUG_SCORE_REJECT:
        verdict = "🔴 BERBAHAYA — Jangan trade!"
    elif rug_score >= config.RUG_SCORE_WARN:
        verdict = "🟡 RISIKO TINGGI — Hati-hati"
    else:
        verdict = "🟢 Risiko relatif rendah"

    msg = (
        f"🛡 *Rug Risk Analysis*\n━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{t['name']}* (${t['symbol']})\n"
        f"CA: `{addr}`\n\n"
        f"*Rug Score: {rug_score}/100*\n"
        f"*Verdict:* {verdict}\n"
        f"*Filter:* {'✅ Lolos' if rug_passed else '❌ '+rug_reason}\n\n"
        f"*Detail:*\n"
        f"├ Top3 holder: {p3}% {'⚠️' if p3 > 40 else '✅'}\n"
        f"├ Top10 holder: {p10}% {'⚠️' if p10 > 60 else '✅'}\n"
        f"├ Wash trading: level {t['wash']} ({t['abpm']}x/wallet) {'🔴' if t['wash']>=2 else '✅'}\n"
        f"├ Liq/MCap: {round(t['liq']/max(t['mcap'],1)*100,1)}% {'⚠️' if t['liq']/max(t['mcap'],1) < 0.03 else '✅'}\n"
        f"├ LP Trend: {'🔴 Ditarik' if liq_trend=='decreasing' else '✅ '+liq_trend}\n"
        f"└ Boosted: {'⚠️ Ya' if t['is_boosted'] else '✅ Tidak'}\n"
    )
    if hwarn:
        msg += f"\n{hwarn}\n"
    msg += f"\n🔗 [Chart]({t['pair_url']})"
    msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{addr})"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    now    = time.time()
    active = sum(1 for t in seen_addresses.values()
                 if (now-t)/3600 <= SEEN_TTL_HOURS)
    aw_hold = sum(1 for d in auto_watchlist.values() if d.get("status")=="HOLD")
    aw_weak = sum(1 for d in auto_watchlist.values() if d.get("status") in ("WEAK",))
    aw_exit = sum(1 for d in auto_watchlist.values()
                 if d.get("status") in ("EXIT","EXIT_WARN"))
    gmgn_s  = "✅ aktif" if config.ENABLE_GMGN_SMART_MONEY else "❌"
    helius_s= "✅" if config.HELIUS_API_KEY else "⚠️ belum diset (opsional)"
    await update.message.reply_text(
        f"✅ *ONLINE v12*\n━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Scan: {config.CHECK_INTERVAL_MINUTES}m\n"
        f"🔁 Seen: {active} (TTL {SEEN_TTL_HOURS}j)\n"
        f"📋 Hold manual: {len(hold_watchlist)}\n"
        f"👁 Auto WL: {len(auto_watchlist)} token\n"
        f"   🟢{aw_hold} hold | 🟡{aw_weak} lemah | 🔴{aw_exit} exit\n"
        f"🛡 Rug Filter: {'✅ AKTIF' if config.ENABLE_RUG_FILTER else '❌'}\n"
        f"🔄 Revival Scan: {'✅ AKTIF' if config.ENABLE_REVIVAL_SCAN else '❌'}\n"
        f"🧠 GMGN SM: {gmgn_s}\n"
        f"🧠 SM wallets: {len(sm_wallets)}\n"
        f"📡 Helius: {helius_s}\n"
        f"📡 WS PumpFun: {'✅'if config.ENABLE_PUMPFUN_WS else'❌'}\n"
        f"👥 Holder check: {'✅'if config.ENABLE_HOLDER_CHECK else'❌'}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"🌙>={config.SCORE_MOONBAG} | 🎯>={config.SCORE_SWING} | ⚡>={config.SCORE_SCALP}\n"
        f"🛡 Rug reject>={config.RUG_SCORE_REJECT} | warn>={config.RUG_SCORE_WARN}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    n = len(seen_addresses); seen_addresses.clear(); save_state()
    await update.message.reply_text(f"✅ Cache reset ({n} entri).")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ══════════════════════════════════════════════════════════════
#  BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════

async def background_scanner(bot):
    while True:
        await asyncio.sleep(config.CHECK_INTERVAL_MINUTES * 60)
        await do_scan(bot, manual=False)

async def background_auto_watchlist(bot):
    await asyncio.sleep(120)
    while True:
        await check_auto_watchlist(bot, silent=True)
        await asyncio.sleep(config.AUTO_WL_CHECK_EVERY * 60)

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
    print("  MEMECOIN MONITOR v12")
    print("=" * 50)
    load_state()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for cmd, fn in [
        ("start","cmd_start"), ("scan","cmd_scan"),
        ("aw","cmd_aw"), ("hold","cmd_hold"),
        ("wallet","cmd_wallet"), ("gmgn","cmd_gmgn"),
        ("holders","cmd_holders"), ("revival","cmd_revival"),
        ("rug","cmd_rug"), ("status","cmd_status"),
        ("clearcache","cmd_clearcache"), ("help","cmd_help"),
    ]:
        app.add_handler(CommandHandler(cmd, globals()[fn]))

    async with app:
        await app.start()
        bot = app.bot

        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                "🤖 *MEMECOIN MONITOR v12*\n━━━━━━━━━━━━━━━━━━━\n"
                "✅ LAPISAN 1: Rug Filter aktif\n"
                "   • Hard reject bundler/wash/holder concentration\n"
                "   • LP drain detection\n"
                "   • Rug Score system (0-100)\n"
                "✅ LAPISAN 2: SM weighted scoring (1.5x)\n"
                "✅ LAPISAN 3: Dead Coin Revival Scanner\n"
                "   • Tangkap token tua yang tiba-tiba hidup\n"
                "   • Bypass downtrend filter untuk revival\n"
                "   • LP trend analysis (legit vs dead cat)\n"
                "✅ LAPISAN 4: Scoring dioverhaul\n"
                "   • Weighted multiplier per tipe\n"
                "   • Revival tipe baru dengan confidence level\n\n"
                "📌 Command baru: /revival <CA> | /rug <CA>\n\n"
                "/scan | /aw | /hold | /status\n"
                "⏳ Starting scan..."
            ),
            parse_mode=ParseMode.MARKDOWN)

        await do_scan(bot, manual=False)

        tasks = [
            background_scanner(bot),
            background_auto_watchlist(bot),
            background_wallet_poller(bot),
        ]
        if config.ENABLE_PUMPFUN_WS:
            tasks += [pumpfun_websocket_listener(), pumpfun_alert_processor(bot)]

        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
