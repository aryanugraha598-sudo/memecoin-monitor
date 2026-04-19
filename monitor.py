"""
MEMECOIN MONITOR v10
Fitur baru:
  - AUTO WATCHLIST: token yang lolos scan dengan score >= threshold
    otomatis masuk auto_watchlist dan dipantau berkala
  - Tracking sejak pertama scan: MCap awal, score awal, harga awal
  - Laporan perubahan: MCap sekarang vs awal, volume naik/turun,
    status masih layak hold atau sudah harus exit
  - Perintah /aw untuk melihat dan kelola auto watchlist
  - Semua fix dari v9 tetap ada
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
volume_history  = {}
seen_addresses  = {}
hold_watchlist  = set()
auto_watchlist  = {}   # addr -> {name, symbol, score, mcap0, v1h0, liq0, ts, sig, ttype, pair_url}
sm_wallets      = []
sm_last_buy     = {}
hold_vol_prev   = {}
pumpfun_bc      = {}
boosted_tokens  = set()
ws_alerted      = {}

SEEN_TTL_HOURS      = 6
AUTO_WL_MAX         = 50    # Maksimal token di auto watchlist
AUTO_WL_SCORE_MIN   = 45    # Score minimum untuk masuk auto watchlist
AUTO_WL_CHECK_EVERY = 15    # Cek auto watchlist setiap N menit
AUTO_WL_MAX_AGE_H   = 72    # Hapus dari auto watchlist setelah 72 jam
STATE_FILE          = "state.json"

# ══════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "seen":          seen_addresses,
                "vol_hist":      {k: list(v) for k, v in volume_history.items()},
                "hold":          list(hold_watchlist),
                "auto_wl":       auto_watchlist,
                "sm_wallets":    sm_wallets,
                "sm_last_buy":   sm_last_buy,
                "pumpfun_bc":    pumpfun_bc,
                "ws_alerted":    ws_alerted,
            }, f)
    except Exception as e:
        print(f"  save_state err: {e}")

def load_state():
    global seen_addresses, volume_history, hold_watchlist
    global auto_watchlist, sm_wallets, sm_last_buy, pumpfun_bc, ws_alerted
    sm_wallets.clear()
    sm_wallets.extend(config.SMART_MONEY_WALLETS)
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        seen_addresses = d.get("seen", {})
        volume_history = {k: [(t, v) for t, v in vs]
                          for k, vs in d.get("vol_hist", {}).items()}
        hold_watchlist = set(d.get("hold", []))
        auto_watchlist = d.get("auto_wl", {})
        sm_last_buy    = d.get("sm_last_buy", {})
        pumpfun_bc     = d.get("pumpfun_bc", {})
        ws_alerted     = d.get("ws_alerted", {})
        for w in d.get("sm_wallets", []):
            if w not in sm_wallets:
                sm_wallets.append(w)
        print(f"  State loaded: {len(seen_addresses)} seen | "
              f"{len(hold_watchlist)} hold | {len(auto_watchlist)} auto_wl | "
              f"{len(sm_wallets)} SM wallets")
    except Exception as e:
        print(f"  load_state err: {e}")


# ══════════════════════════════════════════════════════════════
#  AUTO WATCHLIST HELPERS
# ══════════════════════════════════════════════════════════════

def add_to_auto_watchlist(token, score, sig, ttype):
    addr = token["address"]
    if addr in auto_watchlist:
        return

    if len(auto_watchlist) >= AUTO_WL_MAX:
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
    stale = [
        addr for addr, d in auto_watchlist.items()
        if (now - d.get("ts", 0)) / 3600 > AUTO_WL_MAX_AGE_H
        or d.get("status") == "EXITED"
    ]
    for addr in stale:
        del auto_watchlist[addr]
    if stale:
        print(f"  [AW] Cleaned {len(stale)} stale entries")


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

def track_volume(addr, vol_1h):
    if addr not in volume_history:
        volume_history[addr] = []
    volume_history[addr].append((time.time(), vol_1h))
    volume_history[addr] = volume_history[addr][-12:]

def detect_resurrection(addr, cur_vol):
    hist = volume_history.get(addr, [])
    if len(hist) < 3:
        return False, 0
    prev = [v for _, v in hist[:-1]]
    avg  = sum(prev) / len(prev)
    if avg < 300 and cur_vol > 5000:
        return True, round(cur_vol / max(avg, 1))
    return False, 0


# ══════════════════════════════════════════════════════════════
#  PUMP.FUN WEBSOCKET
# ══════════════════════════════════════════════════════════════

pumpfun_queue = asyncio.Queue()

async def pumpfun_websocket_listener():
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            print(f"  [WS] Connecting to pump.fun...")
            async with websockets.connect(
                    uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeTokenTrade"}))
                print(f"  [WS] Connected! Monitoring trades only.")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        await process_pumpfun_trade(msg)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        print(f"  [WS] msg err: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            print(f"  [WS] Closed: {e}. Retry in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"  [WS] Error: {e}. Retry in 10s...")
            await asyncio.sleep(10)


async def process_pumpfun_trade(msg):
    tx_type = msg.get("txType", "")
    if tx_type not in ("buy", "sell"):
        return
    mint   = msg.get("mint", "")
    name   = msg.get("name", "Unknown")
    symbol = msg.get("symbol", "???")
    if not mint:
        return
    v_sol    = msg.get("vSolInBondingCurve", 0) or 0
    GRAD_SOL = 85.0
    bc_pct   = min(round((v_sol / GRAD_SOL) * 100, 1), 99.9) if v_sol > 0 else 0
    pumpfun_bc[mint] = bc_pct
    if v_sol < config.PUMPFUN_MIN_SOL:
        return
    prev_alerted = ws_alerted.get(mint, 0)
    if bc_pct >= config.PUMPFUN_BC_ALERT_PCT and prev_alerted < config.PUMPFUN_BC_ALERT_PCT:
        ws_alerted[mint] = config.PUMPFUN_BC_ALERT_PCT
        await pumpfun_queue.put({
            "type": "PRE_GRADUATION", "mint": mint, "name": name,
            "symbol": symbol, "v_sol": v_sol, "bc_pct": bc_pct, "ts": time.time(),
        })
        print(f"  [WS] PRE_GRAD: {name} BC={bc_pct}% ({v_sol:.1f} SOL)")
    elif bc_pct >= config.PUMPFUN_BC_MIN_PCT and prev_alerted < config.PUMPFUN_BC_MIN_PCT:
        ws_alerted[mint] = config.PUMPFUN_BC_MIN_PCT
        await pumpfun_queue.put({
            "type": "BC_MILESTONE", "mint": mint, "name": name,
            "symbol": symbol, "v_sol": v_sol, "bc_pct": bc_pct, "ts": time.time(),
        })
        print(f"  [WS] BC_MILE: {name} BC={bc_pct}% ({v_sol:.1f} SOL)")


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
            token = get_token_details(pair, all_urls) if pair else None
            if itype == "PRE_GRADUATION":
                header       = "🚀 PRE-GRADUATION ALERT"
                desc         = f"BC {bc_pct}% — Akan graduate ke Raydium SEBENTAR LAGI!"
                urgency_note = "WINDOW SEMPIT — biasanya pump 2-5x saat graduation"
            else:
                header       = "📈 BC MILESTONE"
                desc         = f"BC {bc_pct}% — Momentum kuat mendekati graduation"
                urgency_note = "Pantau terus, mendekati graduation threshold"
            gb, gs, gi = 0, None, []
            if config.ENABLE_GMGN_SMART_MONEY and pair:
                gb, gs, gi = gmgn_check_smart_money(mint, name)
            msg = (
                f"{header}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{name}* (${symbol})\n"
                f"📋 CA: `{mint}`\n"
                f"💡 {desc}\n"
                f"_{urgency_note}_\n\n"
                f"⛽ *Pump.fun Status*\n"
                f"├ Bonding Curve: *{bc_pct}%*\n"
                f"├ SOL terkumpul: *{round(v_sol, 1)} SOL* / 85 SOL\n"
                f"└ Sisa: ~{round(85 - v_sol, 1)} SOL lagi\n"
            )
            if token:
                msg += (
                    f"\n📈 *Market Data*\n"
                    f"├ MCap: ${token['mcap']:,.0f}\n"
                    f"├ Liq: ${token['liq']:,.0f}\n"
                    f"├ Vol 1h: ${token['v1h']:,.0f}\n"
                    f"└ BSR 1h: {token['h1_bsr']}x\n"
                )
            if gs:
                msg += f"\n🧠 *Smart Money:* {gs}\n"
            if gi:
                lines = [f"  • `{w['wallet'][:6]}...{w['wallet'][-4:]}` [{w['label']}]"
                         for w in gi[:3]]
                msg += "💼 *Insiders:*\n" + "\n".join(lines) + "\n"
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
    if not config.ENABLE_HOLDER_CHECK:
        return True, 0, None
    try:
        headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}
        r = requests.get(
            "https://public-api.solscan.io/token/holders",
            params={"tokenAddress": token_address, "limit": 10, "offset": 0},
            headers=headers, timeout=6)
        if r.status_code != 200:
            return True, 0, None
        holders = r.json().get("data", [])
        if not holders:
            return True, 0, None
        total = sum(float(h.get("amount", 0)) for h in holders)
        if total == 0:
            return True, 0, None
        BURN = {
            "11111111111111111111111111111111",
            "So11111111111111111111111111111111111111112",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        }
        top10_pct = sum(
            float(h.get("amount", 0)) / total * 100
            for h in holders[:10]
            if h.get("owner", "") not in BURN
        )
        is_healthy = top10_pct < 50
        warn = None if is_healthy else f"🔴 Top 10 holder: {round(top10_pct,1)}% supply!"
        return is_healthy, round(top10_pct, 1), warn
    except Exception as e:
        print(f"  Holder err: {e}")
        return True, 0, None


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
        return 0, None, []
    traders = gmgn_get_top_traders(addr)
    if not traders:
        return 0, None, []
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
    if not found:
        return 0, None, []
    emap = {
        "insider": "🔴 Insider", "smart_degen": "🧠 SmDegen",
        "kol": "📢 KOL", "sniper": "🎯 Sniper",
        "whale": "🐋 Whale", "smart": "🧠 Smart",
    }
    counts  = {}
    for l in found:
        counts[l] = counts.get(l, 0) + 1
    parts   = [f"{emap.get(l,l)} x{c}" for l, c in counts.items()]
    summary = " | ".join(parts) + f" (+{total_bonus}pts)"
    print(f"  🧠 GMGN [{name}]: {', '.join(found)}")
    return total_bonus, summary, insiders


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
                        liq  = float(p.get("liquidity", {}).get("usd", 0) or 0)
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
        created_at = pair.get("pairCreatedAt", 0)
        age_h      = (time.time() - created_at / 1000) / 3600 if created_at else 9999
        h1  = txns.get("h1", {})
        h6  = txns.get("h6", {})
        h24 = txns.get("h24", {})
        h1b  = h1.get("buys",0);  h1s  = h1.get("sells",0)
        h6b  = h6.get("buys",0);  h6s  = h6.get("sells",0)
        h24b = h24.get("buys",0); h24s = h24.get("sells",0)
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
        return {
            "name":     base.get("name", "Unknown"),
            "symbol":   base.get("symbol", "???"),
            "address":  addr,
            "mcap":     float(pair.get("marketCap", 0) or 0),
            "liq":      float(liq.get("usd", 0) or 0),
            "v1h": v1h, "v6h": v6h, "v24h": v24h,
            "vol_accel":   vol_accel,
            "pc_5m":  float(pc.get("m5", 0) or 0),
            "pc_1h":  float(pc.get("h1", 0) or 0),
            "pc_6h":  float(pc.get("h6", 0) or 0),
            "pc_24h": float(pc.get("h24", 0) or 0),
            "age_h":   round(age_h, 1),
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
#  FILTER
# ══════════════════════════════════════════════════════════════

def passes_filter(token):
    if token["mcap"] <= 0:
        return False, "MCap invalid"
    if token["liq"] < config.MIN_LIQUIDITY_USD:
        return False, "Liq rendah"
    if token["wash"] >= 3:
        return False, f"Wash ({token['abpm']}x/wallet)"
    if token["v1h"] < config.MIN_VOLUME_1H:
        return False, f"Vol1h<${config.MIN_VOLUME_1H:,}"
    if token["pc_1h"] < -5 and token["vol_accel"] < 1.0:
        return False, f"Distribusi:{token['pc_1h']}%+accel{token['vol_accel']}x"
    if token["pc_1h"] < -10 and token["pc_6h"] < -15:
        return False, f"Downtrend:1h={token['pc_1h']}%,6h={token['pc_6h']}%"
    if token["v24h"] > 0 and token["mcap"] > 0:
        if token["v24h"] / token["mcap"] > 100:
            return False, "Vol/MCap>100x"
    return True, "OK"


# ══════════════════════════════════════════════════════════════
#  SCORING
# ══════════════════════════════════════════════════════════════

def score_token(token):
    score    = 0
    reasons  = []
    warnings = []
    age      = token["age_h"]
    is_res, res_mult = detect_resurrection(token["address"], token["v1h"])

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

    if age < config.AGE_TIER_FRESH:       age_mod = 0
    elif age < config.AGE_TIER_YOUNG:     age_mod = -5
    elif age < config.AGE_TIER_NORMAL:    age_mod = -10
    elif age < config.AGE_TIER_OLD:       age_mod = -20
    else:
        age_mod = -35
        warnings.append(f"⏰ Token tua ({round(age/24,1)} hari)")
    if ttype == "RESURRECTION":
        age_mod = max(age_mod // 3, -5)
    score += age_mod

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

    elif ttype == "RESURRECTION":
        score += 35; reasons.append(f"⚡ RESURRECTION {res_mult}x ({round(age/24,1)} hari)")
        bsr = token["h1_bsr"]
        if bsr > 2:      score += 25; reasons.append(f"💚 Buy pressure ({bsr}x)")
        elif bsr > 1.3:  score += 15; reasons.append(f"🟡 Buy ada ({bsr}x)")
        if token["pc_1h"] > 50:    score += 25; reasons.append(f"🚀 +{token['pc_1h']}% 1h!")
        elif token["pc_1h"] > 20:  score += 12; reasons.append(f"📈 +{token['pc_1h']}%")
        if token["v1h"] > 20000:   score += 15; reasons.append(f"🔥 Vol1h ${token['v1h']:,.0f}")
        if token["makers"] > 0 and token["makers"] < 30:
            score -= 15
            warnings.append(f"⚠️ Makers rendah ({token['makers']}) — res. lemah")

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

    if token["mcap"] > 0:
        lr = token["liq"] / token["mcap"]
        if lr > 0.15:   score += 10; reasons.append(f"💧 Liq sehat ({round(lr*100,1)}%)")
        elif lr > 0.05: score += 5
        elif lr < 0.02: score -= 10; warnings.append("⚠️ Liq tipis!")

    bc = token.get("bc_pct", 0)
    if bc >= 90:   score += 20; reasons.append(f"🔥 BC {bc}% — hampir graduate!")
    elif bc >= 75: score += 10; reasons.append(f"📈 BC {bc}%")

    if token["is_boosted"]:
        score -= 10
        warnings.append("⚠️ Boosted (dev paid promotion)")

    if token["wash"] == 2:   score -= 15; warnings.append(f"🔶 Wash ({token['abpm']}x/wallet)")
    elif token["wash"] == 1: score -= 5;  warnings.append(f"⚠️ Suspicious ({token['abpm']}x/wallet)")
    if token["pc_1h"] < -25: score -= 20; warnings.append(f"📉 Dump -{abs(token['pc_1h'])}% 1h!")
    if token["pc_5m"] > 5:   score += 5;  reasons.append(f"🟢 +{token['pc_5m']}% dalam 5m")

    return score, reasons, warnings, ttype


def get_signal(score, ttype):
    if score >= config.SCORE_MOONBAG:
        d = "MCap kecil + momentum = potensi 100x+" if ttype in ["FRESH_GRADUATE","RESURRECTION"] \
            else "Hold berminggu-minggu"
        return "🌙 MOONBAG CANDIDATE", d
    elif score >= config.SCORE_SWING:
        return "🎯 SWING TARGET", "Hold 1-7 hari"
    elif score >= config.SCORE_SCALP:
        return {
            "FRESH_GRADUATE": ("⚡ SCALP — FRESH GRADUATE", "Baru graduate, tangkap momentum"),
            "RESURRECTION":   ("⚡ SCALP — RESURRECTION",   "Volume bangkit, wave pertama"),
            "MOMENTUM":       ("⚡ SCALP — MOMENTUM",        "Momentum kuat, exit cepat"),
        }.get(ttype, ("⚡ SCALP", "Hold <2 jam"))
    return None, None


# ══════════════════════════════════════════════════════════════
#  EXIT SIGNAL
# ══════════════════════════════════════════════════════════════

def detect_exit_signal(token):
    urgency = 0
    reasons = []
    addr    = token["address"]
    if token["h1_bsr"] < config.EXIT_BSR_DANGER:
        urgency = max(urgency, 2)
        reasons.append(f"🔴 BSR 1h: {token['h1_bsr']} — distribusi berat!")
    if token["pc_1h"] < config.EXIT_DUMP_PCT_1H:
        urgency = max(urgency, 3)
        reasons.append(f"🔴 Dump {token['pc_1h']}% dalam 1 jam!")
    prev = hold_vol_prev.get(addr, token["vol_accel"])
    if prev > 1 and token["vol_accel"] < prev * config.EXIT_VOL_COLLAPSE:
        urgency = max(urgency, 2)
        reasons.append(f"📉 Vol collapse: {prev}x -> {token['vol_accel']}x")
    hold_vol_prev[addr] = token["vol_accel"]
    if token["wash"] >= 2:
        urgency = max(urgency, 1)
        reasons.append("🔶 Wash trading muncul!")
    if token["mcap"] > 0 and token["liq"] / token["mcap"] < 0.02:
        urgency = max(urgency, 2)
        reasons.append("⚠️ Liquidity sangat tipis!")
    return urgency, reasons


# ══════════════════════════════════════════════════════════════
#  AUTO WATCHLIST CHECKER — FITUR UTAMA BARU
# ══════════════════════════════════════════════════════════════

def aw_mcap_change_str(mcap0, mcap_now):
    if mcap0 <= 0:
        return "N/A", ""
    pct = ((mcap_now - mcap0) / mcap0) * 100
    if pct >= 100:   emoji = "🚀🚀"
    elif pct >= 50:  emoji = "🚀"
    elif pct >= 20:  emoji = "📈"
    elif pct >= 0:   emoji = "🟢"
    elif pct >= -20: emoji = "🟡"
    elif pct >= -50: emoji = "🔴"
    else:            emoji = "💀"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{round(pct,1)}%", emoji

def aw_vol_change_str(v0, v_now):
    if v0 <= 0:
        return "N/A"
    ratio = v_now / v0
    if ratio >= 2:    return f"🔥 {round(ratio,1)}x (naik)"
    elif ratio >= 1.1: return f"📈 {round(ratio,1)}x (naik)"
    elif ratio >= 0.8: return f"🟡 Stabil"
    elif ratio >= 0.5: return f"📉 Turun {round((1-ratio)*100,0):.0f}%"
    else:              return f"💀 Drop parah {round((1-ratio)*100,0):.0f}%"

async def check_auto_watchlist(bot, silent=False):
    """
    Cek semua token di auto_watchlist secara berkala.
    silent=True  -> hanya kirim notif jika ada EXIT warning atau pump 50%+
    silent=False -> kirim update untuk semua token
    """
    if not auto_watchlist:
        return

    print(f"\n  [AW] Checking {len(auto_watchlist)} tokens...")
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
            score_now, _, warnings, ttype = score_token(t)
            eu, er = detect_exit_signal(t)

            # Update data terbaru
            auto_watchlist[addr]["mcap_now"]  = t["mcap"]
            auto_watchlist[addr]["v1h_now"]   = t["v1h"]
            auto_watchlist[addr]["score_now"] = score_now
            auto_watchlist[addr]["check_ts"]  = now

            mcap0    = aw.get("mcap0", 0)
            v1h0     = aw.get("v1h0", 0)
            score0   = aw.get("score0", 0)
            held_h   = round((now - aw.get("ts", now)) / 3600, 1)
            name     = aw.get("name", "?")
            symbol   = aw.get("symbol", "?")
            sig0     = aw.get("sig", "?")
            pair_url = t["pair_url"] or aw.get("pair_url", "")

            mc_pct_str, mc_emoji = aw_mcap_change_str(mcap0, t["mcap"])
            vol_str              = aw_vol_change_str(v1h0, t["v1h"])
            mcap_pct_raw = ((t["mcap"] - mcap0) / mcap0 * 100) if mcap0 > 0 else 0

            # Tentukan status
            if eu >= 3:
                status     = "EXIT_NOW"
                auto_watchlist[addr]["status"] = "EXIT"
            elif eu >= 2:
                status     = "EXIT_WARN"
                auto_watchlist[addr]["status"] = "WEAK"
            elif score_now < 20:
                status     = "WEAK"
                auto_watchlist[addr]["status"] = "WEAK"
            else:
                status     = "HOLD"
                auto_watchlist[addr]["status"] = "HOLD"

            urgency_icon = {
                "EXIT_NOW":  "🚨",
                "EXIT_WARN": "⚠️",
                "WEAK":      "🟡",
                "HOLD":      "🟢",
            }.get(status, "🟡")

            # Keputusan apakah kirim pesan
            should_send = (
                not silent           # Mode manual: kirim semua
                or eu >= 2           # Ada exit warning: selalu kirim
                or mcap_pct_raw >= 50  # Pump besar: beritahu
            )

            if not should_send:
                continue

            msg = (
                f"{urgency_icon} *AUTO WATCHLIST UPDATE*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{name}* (${symbol})\n"
                f"CA: `{addr}`\n"
                f"Sinyal awal: _{sig0}_ | Hold: {held_h} jam\n\n"
                f"📊 *Performa sejak scan pertama:*\n"
                f"├ MCap awal:  ${mcap0:,.0f}\n"
                f"├ MCap kini:  ${t['mcap']:,.0f} {mc_emoji} {mc_pct_str}\n"
                f"├ Vol1h awal: ${v1h0:,.0f}\n"
                f"├ Vol1h kini: ${t['v1h']:,.0f} — {vol_str}\n"
                f"├ Score awal: {score0} | Score kini: {score_now}\n"
                f"├ BSR 1h:     {t['h1_bsr']}x\n"
                f"└ 5m:{t['pc_5m']}% | 1h:{t['pc_1h']}% | 6h:{t['pc_6h']}%\n"
            )

            if status == "EXIT_NOW":
                msg += f"\n🚨 *REKOMENDASI: EXIT SEKARANG!*\n"
                if er: msg += "\n".join(er)
            elif status == "EXIT_WARN":
                msg += f"\n⚠️ *WARNING — Pertimbangkan exit:*\n"
                if er: msg += "\n".join(er)
            elif status == "WEAK":
                msg += f"\n🟡 *Momentum melemah* — pantau ketat\n"
                if warnings: msg += "\n".join(warnings[:2])
            elif mcap_pct_raw >= 50:
                msg += f"\n🚀 *Pump signifikan! Pertimbangkan take profit sebagian*\n"
            else:
                msg += f"\n🟢 *Masih layak hold* — kondisi oke\n"

            msg += f"\n\n🔗 [Chart]({pair_url})"
            msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{addr})"
            msg += f"\n_/aw remove {addr[:12]}..._"
            msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            await asyncio.sleep(1.5)

        except Exception as e:
            print(f"  [AW] err {addr[:8]}: {e}")

    cleanup_auto_watchlist()
    save_state()
    print(f"  [AW] Done. {len(auto_watchlist)} tokens tracked.")


# ══════════════════════════════════════════════════════════════
#  ALERT (scan biasa)
# ══════════════════════════════════════════════════════════════

def fmt_age(h):
    if h < 24:    return f"{round(h,1)} jam"
    elif h < 720: return f"{round(h/24,1)} hari"
    else:         return f"{round(h/24/30,1)} bulan"

async def send_alert(bot, token, score, sig, desc,
                     reasons, warnings, ttype,
                     tw=None, gmgn_summary=None,
                     insider_wallets=None,
                     holder_warning=None, holder_pct=0):

    wash_l = ["✅ Organik","🟡 Suspicious","🔶 High Sus","🔴 Wash"][min(token["wash"],3)]
    te     = {"FRESH_GRADUATE":"🆕","RESURRECTION":"⚡",
              "MOMENTUM":"🚀","ACCUMULATION":"📈"}.get(ttype,"📊")
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
        holder_sec = f"\n✅ Holder sehat (top10={holder_pct}%)\n"

    bc_note = f"\n⛽ BC: {token['bc_pct']}%\n" if token.get("bc_pct", 0) > 0 else ""

    msg = (
        f"{sig}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token['name']}* (${token['symbol']}){boost}\n"
        f"{te} *{ttype}* | 📊 Score: *{score}/100*\n"
        f"💡 {desc}\n"
        f"{sm_sec}"
        f"\n📋 CA: `{token['address']}`\n"
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
        f"{holder_sec}{pair_sec}{ins_sec}"
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
        chat_id=config.TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ══════════════════════════════════════════════════════════════
#  HOLD WATCHLIST (manual)
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
            track_volume(t["address"], t["v1h"])
            score, _, warnings, _ = score_token(t)
            eu, er = detect_exit_signal(t)
            gb, gs, _ = gmgn_check_smart_money(addr, t["name"])
            score += gb
            ue = {0:"🟢",1:"🟡",2:"🔶",3:"🔴"}.get(eu,"🟡")
            ut = {0:"AMAN — hold lanjut",1:"HATI-HATI",
                  2:"⚠️ EXIT WARNING",3:"🚨 EXIT SEKARANG!"}.get(eu,"Pantau")
            msg = (
                f"📋 *HOLD MONITOR*\n━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{t['name']}* (${t['symbol']})\n"
                f"{ue} Score: *{score}/100* | {ut}\n"
                f"CA: `{t['address']}`\n\n"
                f"5m:{t['pc_5m']}% | 1h:{t['pc_1h']}% | 6h:{t['pc_6h']}%\n"
                f"├ Vol 1h: ${t['v1h']:,.0f} | Accel: {t['vol_accel']}x\n"
                f"├ BSR 1h: {t['h1_bsr']}x\n"
                f"└ Liq: ${t['liq']:,.0f}\n"
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
                gb, gs, _ = gmgn_check_smart_money(addr, t["name"])
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
                if gs:
                    msg += f"\n🧠 GMGN: {gs}"
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
#  MAIN SCAN
# ══════════════════════════════════════════════════════════════

async def do_scan(bot, manual=False):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{ts}] {'Manual' if manual else 'Auto'} scan")
    if manual:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=f"🔄 *Manual Scan* ({ts} WIB)\n⏳ Scanning...",
            parse_mode=ParseMode.MARKDOWN)

    pairs = get_solana_pairs()
    sent = filtered = low = already = aw_added = 0

    for best_pair, all_urls in pairs:
        t = get_token_details(best_pair, all_urls)
        if not t or not t["address"]:
            continue
        track_volume(t["address"], t["v1h"])

        # Jika sudah di auto watchlist, skip (tidak alert ulang)
        if t["address"] in auto_watchlist:
            already += 1
            continue

        if is_seen(t["address"]):
            already += 1
            continue

        ok, reason = passes_filter(t)
        if not ok:
            print(f"  x {t['name'][:20]:<20} {reason}")
            filtered += 1
            continue

        score, reasons, warnings, ttype = score_token(t)
        sig, desc = get_signal(score, ttype)

        btag = " [BOOST]" if t["is_boosted"] else ""
        print(f"  {ttype[:4]} | {t['name'][:14]:<14} | S:{score:>3} | "
              f"MC:${t['mcap']:>9,.0f} | V1h:${t['v1h']:>7,.0f} | "
              f"BSR:{t['h1_bsr']}{btag}")

        if not sig:
            low += 1
            continue

        mark_seen(t["address"])

        # GMGN
        gb, gs, gi = 0, None, []
        if config.ENABLE_GMGN_SMART_MONEY:
            gb, gs, gi = gmgn_check_smart_money(t["address"], t["name"])
            if gb > 0:
                score += gb
                reasons.append("🧠 Smart Money GMGN detected!")
                sig, desc = get_signal(score, ttype)

        # Holder check
        hok, hpct, hwarn = check_holder_distribution(t["address"])
        if not hok:
            warnings.append(hwarn)
            score -= 15

        # Twitter
        tb, ts_tw = 0, None
        if score >= 55:
            tb, ts_tw = analyze_twitter(t["name"], t["symbol"], t["address"])
            score += tb

        # Otomatis masuk Auto Watchlist jika score cukup
        if score >= AUTO_WL_SCORE_MIN:
            add_to_auto_watchlist(t, score, sig, ttype)
            aw_added += 1

        print(f"  >>> {sig} (s={score})"
              f"{' 🧠' if gb>0 else ''}"
              f"{' [AW]' if score >= AUTO_WL_SCORE_MIN else ''}")

        await send_alert(bot, t, score, sig, desc, reasons, warnings, ttype,
                        ts_tw, gs, gi, hwarn, hpct)
        sent += 1
        await asyncio.sleep(1.5)

    await check_hold_watchlist(bot)

    if manual:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"✅ *Scan Selesai*\n━━━━━━━━━━━━━━━━━━━\n"
                f"📊 {len(pairs)} token | 🚫 {filtered} filtered\n"
                f"📉 {low} low score | 🔁 {already} seen\n"
                f"🔔 {sent} alerts | 👁 {aw_added} masuk Auto WL\n"
                f"📋 Auto WL total: {len(auto_watchlist)} token\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
                + ("\n\n💤 Tidak ada koin memenuhi kriteria." if sent == 0 else "")
            ),
            parse_mode=ParseMode.MARKDOWN)

    cleanup_seen()
    if len(volume_history) > 3000:
        oldest = sorted(volume_history,
                        key=lambda k: volume_history[k][-1][0])[:1000]
        for k in oldest:
            del volume_history[k]
    save_state()
    print(f"  Done: {sent}|{filtered}|{low}|{already}|aw={aw_added}")


# ══════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v10*\n━━━━━━━━━━━━━━━━━━━\n"
        "*/scan* — Scan manual\n"
        "*/aw* — Lihat Auto Watchlist\n"
        "*/aw check* — Cek semua token di Auto WL\n"
        "*/aw remove <CA>* — Hapus dari Auto WL\n"
        "*/aw clear* — Bersihkan semua Auto WL\n"
        "*/hold <CA>* — Tambah hold manual\n"
        "*/hold list* — Lihat hold manual\n"
        "*/hold remove <CA>* — Hapus hold manual\n"
        "*/hold check* — Cek exit signals hold\n"
        "*/wallet add <addr>* — Tambah SM wallet\n"
        "*/wallet list* — SM wallet list\n"
        "*/wallet check* — Poll SM wallets\n"
        "*/gmgn <CA>* — Cek smart money\n"
        "*/holders <CA>* — Cek distribusi holder\n"
        "*/status* — Status bot\n"
        "*/clearcache* — Reset seen cache\n",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await do_scan(context.bot, manual=True)

async def cmd_aw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kelola Auto Watchlist."""
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []

    if args and args[0] == "check":
        if not auto_watchlist:
            await update.message.reply_text("📋 Auto Watchlist kosong.")
            return
        await update.message.reply_text(
            f"🔄 Cek {len(auto_watchlist)} token di Auto WL...")
        await check_auto_watchlist(context.bot, silent=False)
        return

    if args and args[0] == "remove" and len(args) > 1:
        target  = args[1].strip()
        removed = []
        for addr in list(auto_watchlist.keys()):
            if addr == target or addr.startswith(target):
                del auto_watchlist[addr]
                removed.append(addr[:12] + "...")
        if removed:
            save_state()
            await update.message.reply_text(
                "✅ Dihapus dari Auto WL:\n" + "\n".join(removed))
        else:
            await update.message.reply_text("❌ Tidak ditemukan.")
        return

    if args and args[0] == "clear":
        n = len(auto_watchlist)
        auto_watchlist.clear()
        save_state()
        await update.message.reply_text(f"✅ Auto WL dibersihkan ({n} token).")
        return

    # Tampilkan daftar
    if not auto_watchlist:
        await update.message.reply_text(
            "📋 Auto Watchlist kosong.\n"
            "Token score bagus akan otomatis masuk saat scan.")
        return

    now   = time.time()
    lines = []
    for addr, d in sorted(
            auto_watchlist.items(),
            key=lambda x: x[1].get("score_now", 0), reverse=True):
        held_h     = round((now - d.get("ts", now)) / 3600, 1)
        mcap0      = d.get("mcap0", 0)
        mcap_now   = d.get("mcap_now", mcap0)
        mc_pct_str, mc_emoji = aw_mcap_change_str(mcap0, mcap_now)
        status     = d.get("status", "HOLD")
        status_ico = {"HOLD":"🟢","WEAK":"🟡","EXIT":"🔴","EXITED":"⚫"}.get(status,"❓")
        score_now  = d.get("score_now", d.get("score0", 0))
        lines.append(
            f"{status_ico} *{d.get('name','?')}* (${d.get('symbol','?')})\n"
            f"  MC: ${mcap_now:,.0f} {mc_emoji}{mc_pct_str} | S:{score_now} | {held_h}j\n"
            f"  `{addr[:20]}...`"
        )

    header = (
        f"👁 *AUTO WATCHLIST ({len(auto_watchlist)} token)*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 Hold | 🟡 Lemah | 🔴 Exit\n\n"
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
        chunk += "_/aw check — update semua sekarang_"
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
        f"✅ *Hold ditambahkan!*\n🪙 {t['name']} (${t['symbol']})\n"
        f"CA: `{addr}`\nMCap: ${t['mcap']:,.0f}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args or args[0] == "list":
        lines = "\n".join(f"{i+1}. `{w[:12]}...`"
                          for i,w in enumerate(sm_wallets)) or "Kosong"
        hs = "✅" if config.HELIUS_API_KEY else "❌"
        await update.message.reply_text(
            f"🧠 *SM Wallets ({len(sm_wallets)})*\n{lines}\nHelius: {hs}",
            parse_mode=ParseMode.MARKDOWN)
        return
    if args[0] == "add" and len(args) > 1:
        w = args[1].strip()
        if w not in sm_wallets:
            sm_wallets.append(w); save_state()
        await update.message.reply_text(f"✅ Ditambahkan. Total: {len(sm_wallets)}")
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
                "❌ HELIUS\\_API\\_KEY belum diset.\nhttps://dev.helius.xyz/")
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
    bonus, summary, _ = gmgn_check_smart_money(addr)
    msg = f"🧠 *GMGN Report*\nBonus: +{bonus}pts"
    if summary: msg += f"\n{summary}"
    msg += f"\n\n*Top Traders:*\n"
    for i,tr in enumerate(traders[:8]):
        w  = tr.get("address","")
        sh = w[:6]+"..."+w[-4:] if w else "?"
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
    addr = args[0].strip()
    await update.message.reply_text("⏳ Mengecek...")
    ok, pct, warn = check_holder_distribution(addr)
    status = "✅ Sehat" if ok else "🔴 Terkonsentrasi"
    await update.message.reply_text(
        f"👥 *Holder Distribution*\nCA: `{addr}`\n"
        f"Top 10: *{pct}%* supply | {status}\n{warn or ''}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    now    = time.time()
    active = sum(1 for t in seen_addresses.values()
                 if (now-t)/3600 <= SEEN_TTL_HOURS)
    ws_s   = f"✅ BC>={config.PUMPFUN_BC_MIN_PCT}% & >={config.PUMPFUN_BC_ALERT_PCT}% only" \
             if config.ENABLE_PUMPFUN_WS else "❌"
    aw_hold = sum(1 for d in auto_watchlist.values() if d.get("status") == "HOLD")
    aw_weak = sum(1 for d in auto_watchlist.values() if d.get("status") == "WEAK")
    aw_exit = sum(1 for d in auto_watchlist.values() if d.get("status") == "EXIT")
    await update.message.reply_text(
        f"✅ *ONLINE v10*\n━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Scan: {config.CHECK_INTERVAL_MINUTES}m\n"
        f"🔁 Seen: {active} (TTL {SEEN_TTL_HOURS}j)\n"
        f"📋 Hold manual: {len(hold_watchlist)}\n"
        f"👁 Auto WL: {len(auto_watchlist)} token "
        f"(🟢{aw_hold} 🟡{aw_weak} 🔴{aw_exit})\n"
        f"🧠 SM wallets: {len(sm_wallets)}\n"
        f"🔍 GMGN: {'✅'if config.ENABLE_GMGN_SMART_MONEY else'❌'}\n"
        f"📡 WS PumpFun: {ws_s}\n"
        f"👥 Holder check: {'✅'if config.ENABLE_HOLDER_CHECK else'❌'}\n"
        f"📡 Helius: {'✅'if config.HELIUS_API_KEY else'⚠️'}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"🌙>={config.SCORE_MOONBAG}|🎯>={config.SCORE_SWING}|⚡>={config.SCORE_SCALP}",
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
    """Cek auto watchlist setiap AUTO_WL_CHECK_EVERY menit (silent mode)."""
    await asyncio.sleep(120)  # Tunggu 2 menit setelah start baru mulai loop
    while True:
        await check_auto_watchlist(bot, silent=True)
        await asyncio.sleep(AUTO_WL_CHECK_EVERY * 60)

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
    print("  MEMECOIN MONITOR v10")
    print("=" * 50)

    load_state()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for cmd, fn in [
        ("start","cmd_start"), ("scan","cmd_scan"),
        ("aw","cmd_aw"),
        ("hold","cmd_hold"), ("wallet","cmd_wallet"),
        ("gmgn","cmd_gmgn"), ("holders","cmd_holders"),
        ("status","cmd_status"), ("clearcache","cmd_clearcache"),
        ("help","cmd_help"),
    ]:
        app.add_handler(CommandHandler(cmd, globals()[fn]))

    async with app:
        await app.start()
        bot = app.bot

        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                "🤖 *MEMECOIN MONITOR v10*\n━━━━━━━━━━━━━━━━━━━\n"
                f"✅ WS: hanya BC>={config.PUMPFUN_BC_MIN_PCT}% & >={config.PUMPFUN_BC_ALERT_PCT}%\n"
                "✅ NEW\\_TOKEN noise: DIHAPUS\n"
                "✅ Auto Watchlist: AKTIF\n"
                f"✅ Score>={AUTO_WL_SCORE_MIN} otomatis dipantau\n"
                f"✅ Update setiap {AUTO_WL_CHECK_EVERY} menit\n"
                "✅ Alert otomatis: exit signal & pump 50%+\n\n"
                "/scan | /aw | /hold | /status\n"
                "⏳ Starting..."
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
