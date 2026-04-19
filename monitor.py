"""
MEMECOIN MONITOR v8
Upgrades:
  - Pump.fun WebSocket real-time (pumpportal.fun)
  - Bonding curve progress monitor
  - Holder distribution check (Solscan)
  - Exit signal detection untuk hold watchlist
  - Boosted token = penalty, bukan bonus
  - GMGN label matching fix (exact match)
  - Resurrection min-makers validation
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
volume_history  = {}   # address -> [(ts, vol_1h)]
seen_addresses  = {}   # address -> ts (TTL)
hold_watchlist  = set()
sm_wallets      = []
sm_last_buy     = {}
hold_vol_prev   = {}   # address -> last vol_accel (for exit detection)
pumpfun_bc      = {}   # address -> bonding_curve_pct (from WS)
boosted_tokens  = set()# addresses yang sedang di-boost (penalty)

SEEN_TTL_HOURS  = 6
STATE_FILE      = "state.json"


# ══════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "seen": seen_addresses,
                "vol_hist": {k: list(v) for k, v in volume_history.items()},
                "hold": list(hold_watchlist),
                "sm_wallets": sm_wallets,
                "sm_last_buy": sm_last_buy,
                "pumpfun_bc": pumpfun_bc,
            }, f)
    except Exception as e:
        print(f"  save_state err: {e}")

def load_state():
    global seen_addresses, volume_history, hold_watchlist
    global sm_wallets, sm_last_buy, pumpfun_bc
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
        hold_watchlist  = set(d.get("hold", []))
        sm_last_buy     = d.get("sm_last_buy", {})
        pumpfun_bc      = d.get("pumpfun_bc", {})
        for w in d.get("sm_wallets", []):
            if w not in sm_wallets:
                sm_wallets.append(w)
        print(f"  State loaded: {len(seen_addresses)} seen | "
              f"{len(hold_watchlist)} hold | {len(sm_wallets)} SM wallets")
    except Exception as e:
        print(f"  load_state err: {e}")


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
    now = time.time()
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
    prev    = [v for _, v in hist[:-1]]
    avg     = sum(prev) / len(prev)
    # Validasi: perlu rata-rata sebelumnya benar-benar rendah
    if avg < 300 and cur_vol > 5000:
        return True, round(cur_vol / max(avg, 1))
    return False, 0


# ══════════════════════════════════════════════════════════════
#  PUMP.FUN WEBSOCKET — REAL-TIME
# ══════════════════════════════════════════════════════════════

pumpfun_queue = asyncio.Queue()   # Token-token menarik dari WS

async def pumpfun_websocket_listener():
    """
    Connect ke pumpportal.fun WebSocket.
    Subscribe ke: newToken, tokenTrade
    Detect token yang bonding curve mendekati graduation (75-99%).
    """
    uri = "wss://pumpportal.fun/api/data"

    while True:
        try:
            print(f"  [WS] Connecting to pump.fun WebSocket...")
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                # Subscribe ke events
                await ws.send(json.dumps({
                    "method": "subscribeNewToken"
                }))
                await ws.send(json.dumps({
                    "method": "subscribeTokenTrade"
                }))
                print(f"  [WS] Connected & subscribed!")

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                        await process_pumpfun_message(msg)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        print(f"  [WS] Message error: {e}")
                        continue

        except websockets.exceptions.ConnectionClosed as e:
            print(f"  [WS] Disconnected: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"  [WS] Error: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)


async def process_pumpfun_message(msg):
    """
    Process pesan dari pump.fun WS.
    Kirim ke queue kalau bonding curve mendekati graduation.
    """
    mint    = msg.get("mint", "")
    tx_type = msg.get("txType", "")
    name    = msg.get("name", "")
    symbol  = msg.get("symbol", "")

    # Bonding curve progress
    # vSol = SOL yang sudah terkumpul di bonding curve
    v_sol   = msg.get("vSolInBondingCurve", 0) or 0
    # Pump.fun graduation threshold = ~85 SOL
    GRAD_SOL = 85.0
    bc_pct   = min(round((v_sol / GRAD_SOL) * 100, 1), 99.9) if v_sol > 0 else 0

    if mint:
        pumpfun_bc[mint] = bc_pct

    # Filter: hanya kirim ke queue kalau menarik
    if not mint:
        return

    # Token baru lahir
    if tx_type == "create":
        if v_sol >= config.PUMPFUN_MIN_SOL * 0.1:
            await pumpfun_queue.put({
                "type": "NEW_TOKEN",
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "v_sol": v_sol,
                "bc_pct": bc_pct,
                "ts": time.time(),
            })
        return

    # Trade: cek kalau BC mendekati graduation
    if tx_type in ("buy", "sell") and bc_pct >= config.PUMPFUN_BC_MIN_PCT:
        # Hanya alert sekali per threshold
        prev_bc = pumpfun_bc.get(f"{mint}_alerted", 0)

        if bc_pct >= config.PUMPFUN_BC_ALERT_PCT and prev_bc < config.PUMPFUN_BC_ALERT_PCT:
            pumpfun_bc[f"{mint}_alerted"] = bc_pct
            await pumpfun_queue.put({
                "type": "PRE_GRADUATION",
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "v_sol": v_sol,
                "bc_pct": bc_pct,
                "ts": time.time(),
            })
        elif bc_pct >= config.PUMPFUN_BC_MIN_PCT and prev_bc < config.PUMPFUN_BC_MIN_PCT:
            pumpfun_bc[f"{mint}_alerted"] = bc_pct
            await pumpfun_queue.put({
                "type": "BC_MILESTONE",
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "v_sol": v_sol,
                "bc_pct": bc_pct,
                "ts": time.time(),
            })


async def pumpfun_alert_processor(bot):
    """
    Consume dari pumpfun_queue dan kirim alert Telegram.
    Ini jalan paralel dengan scanner utama.
    """
    print("  [WS] Alert processor started")
    while True:
        try:
            item = await asyncio.wait_for(pumpfun_queue.get(), timeout=30)
        except asyncio.TimeoutError:
            continue

        try:
            mint    = item["mint"]
            bc_pct  = item["bc_pct"]
            v_sol   = item["v_sol"]
            name    = item.get("name", "Unknown")
            symbol  = item.get("symbol", "???")
            itype   = item["type"]

            # Coba ambil data dari DexScreener kalau sudah ada pair
            pair, all_urls = fetch_token_best_pair(mint)
            token = get_token_details(pair, all_urls) if pair else None

            if itype == "PRE_GRADUATION":
                emoji  = "🚀🚀"
                header = "PRE-GRADUATION ALERT"
                desc   = f"Bonding curve {bc_pct}% — akan graduate SEBENTAR LAGI!"
            elif itype == "BC_MILESTONE":
                emoji  = "📈"
                header = "BC MILESTONE"
                desc   = f"Bonding curve {bc_pct}% — momentum sedang kuat"
            else:
                emoji  = "🆕"
                header = "NEW TOKEN"
                desc   = f"Baru lahir di pump.fun, BC: {bc_pct}%"

            # GMGN check
            gbonus, gsummary, ginsiders = 0, None, []
            if config.ENABLE_GMGN_SMART_MONEY and pair:
                gbonus, gsummary, ginsiders = gmgn_check_smart_money(mint, name)

            msg = (
                f"{emoji} *{header}*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{name}* (${symbol})\n"
                f"📋 CA: `{mint}`\n"
                f"💡 {desc}\n\n"
                f"⛽ *Pump.fun Data*\n"
                f"├ Bonding Curve: *{bc_pct}%*\n"
                f"├ SOL terkumpul: {round(v_sol, 1)} SOL\n"
                f"└ Status: {'🔥 HAMPIR GRADUATE!' if bc_pct >= 90 else '📈 Akumulasi'}\n"
            )

            if token:
                msg += (
                    f"\n📈 *DexScreener Data*\n"
                    f"├ MCap: ${token['mcap']:,.0f}\n"
                    f"├ Liquidity: ${token['liquidity_usd']:,.0f}\n"
                    f"├ Vol 1h: ${token['v1h']:,.0f}\n"
                    f"└ Buy/Sell 1h: {token['h1_bsr']}x\n"
                )

            if gsummary:
                msg += f"\n🧠 *Smart Money:*\n{gsummary}\n"

            if ginsiders:
                lines = []
                for iw in ginsiders[:3]:
                    short = iw["wallet"][:6] + "..." + iw["wallet"][-4:]
                    lines.append(f"  • `{short}` [{iw['label']}]")
                msg += "\n💼 *Insider:*\n" + "\n".join(lines) + "\n"

            msg += f"\n🔗 [Pump.fun](https://pump.fun/{mint})"
            if token:
                msg += f" | [DexScreener]({token['pair_url']})"
            msg += f"\n🔍 [GMGN](https://gmgn.ai/sol/token/{mint})"
            msg += f"\n⏰ {datetime.now().strftime('%H:%M:%S')} WIB"

            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )

        except Exception as e:
            print(f"  [WS] Alert processor err: {e}")

        await asyncio.sleep(0.5)


# ══════════════════════════════════════════════════════════════
#  HOLDER DISTRIBUTION CHECK (Solscan)
# ══════════════════════════════════════════════════════════════

def check_holder_distribution(token_address):
    """
    Cek distribusi holder via Solscan public API.
    Return: (is_healthy, concentration_pct, warning_msg)
    """
    if not config.ENABLE_HOLDER_CHECK:
        return True, 0, None
    try:
        url = f"https://public-api.solscan.io/token/holders"
        params = {"tokenAddress": token_address, "limit": 10, "offset": 0}
        headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}
        r = requests.get(url, params=params, headers=headers, timeout=6)
        if r.status_code != 200:
            return True, 0, None  # Kalau gagal, jangan block

        data = r.json()
        holders = data.get("data", [])
        if not holders:
            return True, 0, None

        total_supply = sum(float(h.get("amount", 0)) for h in holders)
        if total_supply == 0:
            return True, 0, None

        # Exclude alamat burn/sistem yang umum
        BURN_ADDRS = {
            "11111111111111111111111111111111",
            "So11111111111111111111111111111111111111112",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        }

        top10_pct = 0
        real_holders = []
        for h in holders[:10]:
            owner = h.get("owner", "")
            if owner in BURN_ADDRS:
                continue
            pct = float(h.get("amount", 0)) / total_supply * 100
            real_holders.append((owner, pct))
            top10_pct += pct

        # Sehat jika top10 < 50%
        is_healthy = top10_pct < 50
        warning    = None if is_healthy else f"🔴 Top 10 holder: {round(top10_pct, 1)}% supply!"

        return is_healthy, round(top10_pct, 1), warning

    except Exception as e:
        print(f"  Holder check err: {e}")
        return True, 0, None


# ══════════════════════════════════════════════════════════════
#  GMGN SMART MONEY
# ══════════════════════════════════════════════════════════════

GMGN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Referer": "https://gmgn.ai/",
}

def gmgn_get_top_traders(token_address, limit=20):
    try:
        url = f"https://gmgn.ai/defi/quotation/v1/tokens/top_traders/sol/{token_address}"
        r   = requests.get(url, headers=GMGN_HEADERS,
                           params={"limit": limit}, timeout=8)
        if r.status_code != 200:
            return []
        return r.json().get("data", {}).get("items", [])
    except Exception as e:
        print(f"  GMGN err: {e}")
        return []

def gmgn_check_smart_money(token_address, token_name=""):
    if not config.ENABLE_GMGN_SMART_MONEY:
        return 0, None, []

    traders = gmgn_get_top_traders(token_address)
    if not traders:
        return 0, None, []

    total_bonus    = 0
    found_labels   = []
    insider_wallets= []

    for trader in traders:
        tags = [str(t).lower().strip() for t in (trader.get("tags") or [])]

        for label_key, bonus in config.GMGN_LABEL_SCORES.items():
            # FIX: exact match, bukan substring
            if label_key in tags:
                total_bonus += bonus
                found_labels.append(label_key)
                wallet = trader.get("address", "")
                if wallet:
                    insider_wallets.append({
                        "wallet": wallet,
                        "label":  label_key,
                        "pnl":    trader.get("realized_profit", 0) or 0,
                    })
                break

    total_bonus = min(total_bonus, config.GMGN_MAX_BONUS)
    if not found_labels:
        return 0, None, []

    label_map = {
        "insider": "🔴 Insider", "smart_degen": "🧠 SmDegen",
        "kol": "📢 KOL", "sniper": "🎯 Sniper",
        "whale": "🐋 Whale", "smart": "🧠 Smart",
    }
    counts = {}
    for l in found_labels:
        counts[l] = counts.get(l, 0) + 1
    parts = [f"{label_map.get(l, l)} x{c}" for l, c in counts.items()]
    summary = " | ".join(parts) + f" (+{total_bonus}pts)"

    print(f"  🧠 GMGN [{token_name}]: {', '.join(found_labels)}")
    return total_bonus, summary, insider_wallets


# ══════════════════════════════════════════════════════════════
#  NITTER
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
                    clean = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', m)).strip()
                    if len(clean) > 10:
                        tweets.append(clean)
                if tweets:
                    break
            except:
                continue
        if tweets:
            break

    if not tweets:
        return 0, None   # Tidak ada mention = tidak ada info, bukan penalty

    pos = ["moon","gem","100x","buy","bullish","🔥","🚀","💎","alpha","early","send","launch"]
    neg = ["rug","scam","dump","rekt","honeypot","avoid","warning","sus","fake"]
    p = sum(1 for t in tweets for w in pos if w in t.lower())
    n = sum(1 for t in tweets for w in neg if w in t.lower())

    bonus = min(len(tweets) * 2, 10)
    sentiment = "🟢 Positif" if p > n else "🔴 Negatif" if n > p else "🟡 Netral"
    if n > p:
        bonus -= 10
    return bonus, f"📱 Twitter: {len(tweets)} mention | {sentiment}"


# ══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════

def fetch_token_best_pair(token_addr):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8)
        if r.status_code != 200:
            return None, []
        all_p = r.json().get("pairs", [])
        sol_p = [p for p in all_p if p.get("chainId") == "solana"]
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
    """
    Fetch dari DexScreener.
    CATATAN: Boosted tokens sekarang masuk set boosted_tokens untuk penalty.
    """
    token_map  = {}
    seen_token = set()
    boosted_tokens.clear()

    def add(addr, pair, urls):
        if not addr or addr in seen_token:
            return
        seen_token.add(addr)
        cur_liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        existing = token_map.get(addr)
        if not existing:
            token_map[addr] = (pair, urls)
        else:
            ex_liq = float(existing[0].get("liquidity", {}).get("usd", 0) or 0)
            if cur_liq > ex_liq:
                token_map[addr] = (pair, urls)

    def fetch_and_add(addr):
        if addr in seen_token:
            return
        p, urls = fetch_token_best_pair(addr)
        if p:
            add(addr, p, urls)
        time.sleep(0.15)

    # EP1: Token profiles (graduates)
    try:
        r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        if r.status_code == 200:
            tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Profiles: {len(tokens)}")
            for t in tokens[:25]:
                fetch_and_add(t.get("tokenAddress", ""))
    except Exception as e:
        print(f"  EP1 err: {e}")

    # EP2: Boosted — tandai sebagai boosted (penalty)
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        if r.status_code == 200:
            tokens = [t for t in r.json() if t.get("chainId") == "solana"]
            print(f"  Boosted: {len(tokens)}")
            for t in tokens[:20]:
                addr = t.get("tokenAddress", "")
                if addr:
                    boosted_tokens.add(addr)   # ← tandai, bukan skip
                fetch_and_add(addr)
    except Exception as e:
        print(f"  EP2 err: {e}")

    # EP3 & EP4: Direct search
    for query in ["pump sol", "raydium"]:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={query}",
                timeout=10)
            if r.status_code == 200:
                for p in r.json().get("pairs", []):
                    if p.get("chainId") == "solana":
                        addr = p.get("baseToken", {}).get("address", "")
                        liq  = float(p.get("liquidity", {}).get("usd", 0) or 0)
                        urls = [(p.get("dexId","?"), p.get("url",""), liq)]
                        add(addr, p, urls)
        except Exception as e:
            print(f"  EP search err: {e}")

    print(f"  Unique tokens: {len(token_map)} | Boosted: {len(boosted_tokens)}")
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

        h1b  = h1.get("buys",0);   h1s  = h1.get("sells",0)
        h6b  = h6.get("buys",0);   h6s  = h6.get("sells",0)
        h24b = h24.get("buys",0);  h24s = h24.get("sells",0)

        makers = (h24.get("makers") or h6.get("makers") or
                  h1.get("makers") or pair.get("makers") or 0)

        v1h  = float(vol.get("h1",0) or 0)
        v6h  = float(vol.get("h6",0) or 0)
        v24h = float(vol.get("h24",0) or 0)

        avg6h     = v6h / 6 if v6h > 0 else 0
        vol_accel = round(v1h / avg6h, 2) if avg6h > 50 else 0

        wash = 0; abpm = 0
        if makers > 0 and h24b > 0:
            abpm = h24b / makers
            if abpm > 6:     wash = 3
            elif abpm > 4:   wash = 2
            elif abpm > 2.5: wash = 1

        addr = base.get("address", "")
        bc   = pumpfun_bc.get(addr, 0)

        return {
            "name":     base.get("name","Unknown"),
            "symbol":   base.get("symbol","???"),
            "address":  addr,
            "mcap":     float(pair.get("marketCap",0) or 0),
            "liq":      float(liq.get("usd",0) or 0),
            "v1h": v1h, "v6h": v6h, "v24h": v24h,
            "vol_accel":  vol_accel,
            "pc_5m":  float(pc.get("m5",0) or 0),
            "pc_1h":  float(pc.get("h1",0) or 0),
            "pc_6h":  float(pc.get("h6",0) or 0),
            "pc_24h": float(pc.get("h24",0) or 0),
            "age_h":   round(age_h, 1),
            "h1_bsr":  round(h1b  / max(h1s,  1), 2),
            "h6_bsr":  round(h6b  / max(h6s,  1), 2),
            "h24_bsr": round(h24b / max(h24s, 1), 2),
            "makers":  makers,
            "wash":    wash,
            "abpm":    round(abpm, 1),
            "bc_pct":  bc,
            "is_boosted": addr in boosted_tokens,
            "pair_url": pair.get("url",""),
            "all_pair_urls": all_urls or [],
            "dex_id":  pair.get("dexId",""),
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
        return False, "Liquidity rendah"
    if token["wash"] >= 3:
        return False, f"Wash trading ({token['abpm']}x/wallet)"
    if token["v1h"] < config.MIN_VOLUME_1H:
        return False, f"Vol 1h < ${config.MIN_VOLUME_1H:,}"
    if token["pc_1h"] < -5 and token["vol_accel"] < 1.0:
        return False, f"Distribusi: {token['pc_1h']}% + accel {token['vol_accel']}x"
    if token["pc_1h"] < -10 and token["pc_6h"] < -15:
        return False, f"Downtrend: 1h={token['pc_1h']}% 6h={token['pc_6h']}%"
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
    age      = token["age_h"]

    is_res, res_mult = detect_resurrection(token["address"], token["v1h"])

    # Tipe token
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

    # Age modifier
    if age < config.AGE_TIER_FRESH:        age_mod = 0
    elif age < config.AGE_TIER_YOUNG:      age_mod = -5
    elif age < config.AGE_TIER_NORMAL:     age_mod = -10
    elif age < config.AGE_TIER_OLD:        age_mod = -20
    else:
        age_mod = -35
        warnings.append(f"⏰ Token tua ({round(age/24,1)} hari)")

    if ttype == "RESURRECTION":
        age_mod = max(age_mod // 3, -5)

    score += age_mod

    # Score by type
    if ttype == "FRESH_GRADUATE":
        score += 20; reasons.append(f"🆕 Fresh graduate ({round(age,1)} jam)")
        bsr = token["h1_bsr"]
        if bsr > 3:      score += 30; reasons.append(f"💚 Buy pressure kuat ({bsr}x)")
        elif bsr > 2:    score += 20; reasons.append(f"✅ Buy dominan ({bsr}x)")
        elif bsr > 1.3:  score += 10; reasons.append(f"🟡 Buy moderate ({bsr}x)")
        if token["v1h"] > 50000:    score += 20; reasons.append(f"🔥 Vol 1h ${token['v1h']:,.0f}")
        elif token["v1h"] > 10000:  score += 12; reasons.append(f"📈 Vol 1h ${token['v1h']:,.0f}")
        elif token["v1h"] > 3000:   score += 5
        if token["mcap"] < 100000:  score += 15; reasons.append(f"💰 MCap kecil ${token['mcap']:,.0f}")
        elif token["mcap"] < 500000:score += 8;  reasons.append(f"💰 MCap ${token['mcap']:,.0f}")

    elif ttype == "RESURRECTION":
        score += 35; reasons.append(f"⚡ RESURRECTION {res_mult}x ({round(age/24,1)} hari)")
        bsr = token["h1_bsr"]
        if bsr > 2:      score += 25; reasons.append(f"💚 Buy pressure ({bsr}x)")
        elif bsr > 1.3:  score += 15; reasons.append(f"🟡 Buy ada ({bsr}x)")
        if token["pc_1h"] > 50:   score += 25; reasons.append(f"🚀 +{token['pc_1h']}% 1h!")
        elif token["pc_1h"] > 20: score += 12; reasons.append(f"📈 +{token['pc_1h']}%")
        if token["v1h"] > 20000:  score += 15; reasons.append(f"🔥 Vol 1h ${token['v1h']:,.0f}")
        # Validasi resurrection: perlu cukup makers
        if token["makers"] > 0 and token["makers"] < 30:
            score -= 15; warnings.append(f"⚠️ Makers rendah ({token['makers']}) — validasi resurrection lemah")

    elif ttype == "MOMENTUM":
        score += 10; reasons.append(f"🚀 Momentum +{token['pc_1h']}%")
        bsr = token["h1_bsr"]
        if bsr > 3:      score += 25; reasons.append(f"💚 Buy pressure ({bsr}x)")
        elif bsr > 2:    score += 18; reasons.append(f"✅ Buy dominan ({bsr}x)")
        if token["vol_accel"] > 4: score += 20; reasons.append(f"🔥 Vol accel {token['vol_accel']}x")
        elif token["vol_accel"] > 2: score += 12; reasons.append(f"📊 Vol accel {token['vol_accel']}x")

    else:
        if token["vol_accel"] > 3:   score += 20; reasons.append(f"📊 Vol accel {token['vol_accel']}x")
        elif token["vol_accel"] > 2: score += 12; reasons.append(f"📊 Vol accel {token['vol_accel']}x")
        bsr = token["h1_bsr"]
        if bsr > 2.5:    score += 20; reasons.append(f"💚 Buy dominan ({bsr}x)")
        elif bsr > 1.5:  score += 12; reasons.append(f"🟡 Buy dominan ({bsr}x)")
        elif bsr < 0.7:  score -= 20; warnings.append("🔴 Sell pressure tinggi!")

    # Universal
    if token["mcap"] > 0:
        lr = token["liq"] / token["mcap"]
        if lr > 0.15:    score += 10; reasons.append(f"💧 Liq sehat ({round(lr*100,1)}%)")
        elif lr > 0.05:  score += 5
        elif lr < 0.02:  score -= 10; warnings.append("⚠️ Liq tipis!")

    # Bonding curve bonus
    bc = token.get("bc_pct", 0)
    if bc >= 90:     score += 20; reasons.append(f"🔥 BC {bc}% — hampir graduate!")
    elif bc >= 75:   score += 10; reasons.append(f"📈 BC {bc}%")

    # Boosted = penalty (dev bayar untuk visibility = butuh exit liquidity)
    if token["is_boosted"]:
        score -= 10
        warnings.append("⚠️ Token ini di-boost di DexScreener (dev paid promotion)")

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
#  EXIT SIGNAL DETECTION
# ══════════════════════════════════════════════════════════════

def detect_exit_signal(token):
    """
    Deteksi sinyal exit untuk hold watchlist.
    Return: (exit_urgency, reasons)
    exit_urgency: 0=aman, 1=hati-hati, 2=exit warning, 3=EXIT SEKARANG
    """
    urgency = 0
    reasons = []
    addr    = token["address"]

    # Cek BSR
    if token["h1_bsr"] < config.EXIT_BSR_DANGER:
        urgency = max(urgency, 2)
        reasons.append(f"🔴 BSR 1h: {token['h1_bsr']} — distribusi berat!")

    # Cek price dump
    if token["pc_1h"] < config.EXIT_DUMP_PCT_1H:
        urgency = max(urgency, 3)
        reasons.append(f"🔴 Dump {token['pc_1h']}% dalam 1 jam!")

    # Cek volume collapse
    prev_accel = hold_vol_prev.get(addr, token["vol_accel"])
    if prev_accel > 1 and token["vol_accel"] < prev_accel * config.EXIT_VOL_COLLAPSE:
        urgency = max(urgency, 2)
        reasons.append(f"📉 Volume collapse: {prev_accel}x → {token['vol_accel']}x")

    # Update history
    hold_vol_prev[addr] = token["vol_accel"]

    # Cek wash trading muncul
    if token["wash"] >= 2:
        urgency = max(urgency, 1)
        reasons.append(f"🔶 Wash trading muncul!")

    # Cek liquidity tipis
    if token["mcap"] > 0 and token["liq"] / token["mcap"] < 0.02:
        urgency = max(urgency, 2)
        reasons.append(f"⚠️ Liquidity sangat tipis!")

    return urgency, reasons


# ══════════════════════════════════════════════════════════════
#  ALERT FORMATTER
# ══════════════════════════════════════════════════════════════

def fmt_age(h):
    if h < 24:   return f"{round(h,1)} jam"
    elif h < 720: return f"{round(h/24,1)} hari"
    else:         return f"{round(h/24/30,1)} bulan"

async def send_alert(bot, token, score, sig, desc,
                     reasons, warnings, ttype,
                     tw=None, gmgn_summary=None,
                     insider_wallets=None,
                     holder_warning=None, holder_pct=0):

    wash_l  = ["✅ Organik","🟡 Suspicious","🔶 High Sus","🔴 Wash"][min(token["wash"],3)]
    te = {"FRESH_GRADUATE":"🆕","RESURRECTION":"⚡","MOMENTUM":"🚀","ACCUMULATION":"📈"}.get(ttype,"📊")
    boost_note = "\n⚠️ _Boosted (dev promotion)_" if token["is_boosted"] else ""

    sm_sec = f"\n🧠 *SM (GMGN):* {gmgn_summary}\n" if gmgn_summary else ""

    ins_sec = ""
    if insider_wallets:
        lines = []
        for iw in insider_wallets[:3]:
            sh  = iw["wallet"][:6]+"..."+iw["wallet"][-4:]
            pnl = iw["pnl"]
            lines.append(f"  • `{sh}` [{iw['label']}] PNL:{'+'if pnl>=0 else''}${abs(pnl):,.0f}")
        ins_sec = "\n💼 *Insiders:*\n" + "\n".join(lines) + "\n"

    pair_sec = ""
    if len(token["all_pair_urls"]) > 1:
        lines = [f"{'★'if i==0 else' '} [{d}]({u}) Liq:${l:,.0f}"
                 for i,(d,u,l) in enumerate(token["all_pair_urls"][:3])]
        pair_sec = "\n🔀 *Pairs:*\n" + "\n".join(lines) + "\n"

    holder_sec = ""
    if holder_warning:
        holder_sec = f"\n{holder_warning} (top10={holder_pct}%)\n"
    elif holder_pct > 0:
        holder_sec = f"\n✅ Holder distribusi sehat (top10={holder_pct}%)\n"

    bc_note = f"\n⛽ *BC Progress:* {token['bc_pct']}%\n" if token.get("bc_pct", 0) > 0 else ""

    msg = (
        f"{sig}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{token['name']}* (${token['symbol']}){boost_note}\n"
        f"{te} Type: *{ttype}*  |  📊 Score: *{score}/100*\n"
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
        f"{holder_sec}"
        f"{pair_sec}"
        f"{ins_sec}"
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
#  HOLD WATCHLIST MONITOR
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
            score, reasons, warnings, ttype = score_token(t)
            exit_urgency, exit_reasons = detect_exit_signal(t)

            # GMGN check
            gb, gs, _ = gmgn_check_smart_money(addr, t["name"])
            score += gb

            u_emoji = {0:"🟢", 1:"🟡", 2:"🔶", 3:"🔴"}.get(exit_urgency,"🟡")
            u_text  = {
                0: "Kondisi AMAN — hold lanjut",
                1: "HATI-HATI — pantau ketat",
                2: "⚠️ EXIT WARNING",
                3: "🚨 EXIT SEKARANG!"
            }.get(exit_urgency, "Pantau")

            msg = (
                f"📋 *HOLD MONITOR*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *{t['name']}* (${t['symbol']})\n"
                f"{u_emoji} Score: *{score}/100* | {u_text}\n"
                f"CA: `{t['address']}`\n\n"
                f"5m:{t['pc_5m']}% | 1h:{t['pc_1h']}% | 6h:{t['pc_6h']}%\n"
                f"├ Vol 1h: ${t['v1h']:,.0f} | Accel: {t['vol_accel']}x\n"
                f"├ BSR 1h: {t['h1_bsr']}x\n"
                f"└ Liq: ${t['liq']:,.0f} | MCap: ${t['mcap']:,.0f}\n"
            )
            if gs:
                msg += f"\n🧠 GMGN: {gs}\n"
            if exit_reasons:
                msg += "\n🚨 *Exit Signals:*\n" + "\n".join(exit_reasons)
            if warnings:
                msg += "\n\n⚠️ " + " | ".join(warnings)

            msg += f"\n\n🔗 [Chart]({t['pair_url']}) | [GMGN](https://gmgn.ai/sol/token/{addr})"
            msg += f"\n_/hold remove {addr}_"

            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
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
                    buys.append({"address":mint,"wallet":wallet,"timestamp":tx.get("timestamp",0)})
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
                    f"🧠 *SMART MONEY ALERT*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
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
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=msg,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
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

    pairs    = get_solana_pairs()
    sent = filtered = low = already = 0

    for best_pair, all_urls in pairs:
        t = get_token_details(best_pair, all_urls)
        if not t or not t["address"]:
            continue

        track_volume(t["address"], t["v1h"])

        if is_seen(t["address"]):
            already += 1
            continue

        ok, reason = passes_filter(t)
        if not ok:
            print(f"  ✗ {t['name'][:20]:<20} {reason}")
            filtered += 1
            continue

        score, reasons, warnings, ttype = score_token(t)
        sig, desc = get_signal(score, ttype)

        boost_tag = " [BOOST⚠️]" if t["is_boosted"] else ""
        print(f"  {ttype[:4]} | {t['name'][:14]:<14} | S:{score:>3} | "
              f"MC:${t['mcap']:>9,.0f} | V1h:${t['v1h']:>7,.0f} | "
              f"BSR:{t['h1_bsr']}{boost_tag}")

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
                reasons.append(f"🧠 Smart Money GMGN!")
                sig, desc = get_signal(score, ttype)

        # Holder check
        holder_ok, holder_pct, holder_warn = check_holder_distribution(t["address"])
        if not holder_ok:
            warnings.append(holder_warn)
            score -= 15

        # Twitter
        tw_b, tw_s = 0, None
        if score >= 55:
            tw_b, tw_s = analyze_twitter(t["name"], t["symbol"], t["address"])
            score += tw_b

        print(f"  >>> {sig} (s={score})"
              f"{' 🧠' if gb>0 else ''}"
              f"{' ⚠️holder' if not holder_ok else ''}")

        await send_alert(bot, t, score, sig, desc, reasons, warnings, ttype,
                        tw_s, gs, gi, holder_warn, holder_pct)
        sent += 1
        await asyncio.sleep(1.5)

    await check_hold_watchlist(bot)

    if manual:
        gmgn_s = "✅" if config.ENABLE_GMGN_SMART_MONEY else "❌"
        ws_s   = "✅" if config.ENABLE_PUMPFUN_WS else "❌"
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"✅ *Scan Selesai*\n━━━━━━━━━━━━━━━━━━━\n"
                f"📊 {len(pairs)} token | 🚫 {filtered} filtered\n"
                f"📉 {low} low score | 🔁 {already} seen\n"
                f"🔔 {sent} alerts\n"
                f"🔍 GMGN: {gmgn_s} | WS: {ws_s}\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB"
                + ("\n\n💤 Tidak ada koin memenuhi kriteria." if sent == 0 else "")
            ),
            parse_mode=ParseMode.MARKDOWN)

    cleanup_seen()
    if len(volume_history) > 3000:
        oldest = sorted(volume_history, key=lambda k: volume_history[k][-1][0])[:1000]
        for k in oldest:
            del volume_history[k]
    save_state()
    print(f"  Done: {sent} alerts | {filtered} filtered | {low} low | {already} seen")


# ══════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEMECOIN MONITOR v8*\n━━━━━━━━━━━━━━━━━━━\n"
        "*/scan* — Scan manual\n"
        "*/hold <CA>* — Tambah hold watchlist\n"
        "*/hold list* — Lihat hold list\n"
        "*/hold remove <CA>* — Hapus hold\n"
        "*/hold check* — Cek exit signals sekarang\n"
        "*/wallet add <addr>* — Tambah SM wallet\n"
        "*/wallet list* — SM wallet list\n"
        "*/wallet check* — Poll SM wallets\n"
        "*/gmgn <CA>* — Cek smart money manual\n"
        "*/holders <CA>* — Cek distribusi holder\n"
        "*/status* — Status bot\n"
        "*/clearcache* — Reset seen cache\n",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    await do_scan(context.bot, manual=True)

async def cmd_hold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args:
        if not hold_watchlist:
            await update.message.reply_text("📋 Watchlist kosong.\n/hold <CA>")
        else:
            lines = "\n".join(f"• `{a[:8]}...{a[-4:]}`" for a in hold_watchlist)
            await update.message.reply_text(
                f"📋 *Hold ({len(hold_watchlist)}):*\n{lines}", parse_mode=ParseMode.MARKDOWN)
        return
    if args[0] == "list":
        await cmd_hold(update, context)
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
        status = "✅" if config.HELIUS_API_KEY else "❌ (Helius belum diset)"
        lines  = "\n".join(f"{i+1}. `{w[:12]}...`" for i,w in enumerate(sm_wallets)) or "Kosong"
        await update.message.reply_text(
            f"🧠 *SM Wallets ({len(sm_wallets)})*\n{lines}\nHelius: {status}",
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
        await update.message.reply_text(f"✅ Dihapus.")
        return
    if args[0] == "check":
        if not config.HELIUS_API_KEY:
            await update.message.reply_text("❌ HELIUS_API_KEY belum diset.\nhttps://dev.helius.xyz/")
            return
        await update.message.reply_text(f"🔄 Polling {len(sm_wallets)} wallets...")
        await poll_smart_money_wallets(context.bot)

async def cmd_gmgn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /gmgn <CA>")
        return
    addr    = args[0].strip()
    traders = gmgn_get_top_traders(addr, limit=10)
    if not traders:
        await update.message.reply_text("❌ Tidak ada data GMGN.")
        return
    bonus, summary, _ = gmgn_check_smart_money(addr)
    msg  = f"🧠 *GMGN Report*\nBonus: +{bonus}pts"
    if summary: msg += f"\n{summary}"
    msg += f"\n\n*Top {len(traders[:8])} Traders:*\n"
    for i,tr in enumerate(traders[:8]):
        w   = tr.get("address","")
        sh  = w[:6]+"..."+w[-4:] if w else "?"
        pnl = tr.get("realized_profit",0) or 0
        tgs = ", ".join(str(t) for t in (tr.get("tags") or [])[:3]) or "—"
        msg += f"{i+1}. `{sh}` | {'+'if pnl>=0 else''}${abs(pnl):,.0f} | _{tgs}_\n"
    msg += f"\n🔗 [GMGN](https://gmgn.ai/sol/token/{addr})"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def cmd_holders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /holders <CA>")
        return
    addr = args[0].strip()
    await update.message.reply_text("⏳ Mengecek holder distribution...")
    ok, pct, warn = check_holder_distribution(addr)
    status = "✅ Sehat" if ok else "🔴 Terkonsentrasi"
    await update.message.reply_text(
        f"👥 *Holder Distribution*\nCA: `{addr}`\n"
        f"Top 10 holder: *{pct}%* supply\n"
        f"Status: {status}\n"
        f"{warn or ''}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID): return
    now   = time.time()
    active= sum(1 for t in seen_addresses.values() if (now-t)/3600 <= SEEN_TTL_HOURS)
    await update.message.reply_text(
        f"✅ *ONLINE v8*\n━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Scan: {config.CHECK_INTERVAL_MINUTES}m\n"
        f"🔁 Seen: {active} (TTL {SEEN_TTL_HOURS}j)\n"
        f"📋 Hold: {len(hold_watchlist)}\n"
        f"🧠 SM wallets: {len(sm_wallets)}\n"
        f"🔍 GMGN: {'✅'if config.ENABLE_GMGN_SMART_MONEY else'❌'}\n"
        f"📡 WS PumpFun: {'✅'if config.ENABLE_PUMPFUN_WS else'❌'}\n"
        f"👥 Holder check: {'✅'if config.ENABLE_HOLDER_CHECK else'❌'}\n"
        f"🚪 Exit monitor: {'✅'if config.ENABLE_EXIT_MONITOR else'❌'}\n"
        f"📡 Helius: {'✅'if config.HELIUS_API_KEY else'⚠️'}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')} WIB\n\n"
        f"🌙≥{config.SCORE_MOONBAG}|🎯≥{config.SCORE_SWING}|⚡≥{config.SCORE_SCALP}",
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
    print("  MEMECOIN MONITOR v8")
    print("=" * 50)

    load_state()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    for cmd, fn in [
        ("start","cmd_start"),("scan","cmd_scan"),("hold","cmd_hold"),
        ("wallet","cmd_wallet"),("gmgn","cmd_gmgn"),("holders","cmd_holders"),
        ("status","cmd_status"),("clearcache","cmd_clearcache"),("help","cmd_help"),
    ]:
        app.add_handler(CommandHandler(cmd, globals()[fn]))

    async with app:
        await app.start()
        bot = app.bot

        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                "🤖 *MEMECOIN MONITOR v8*\n━━━━━━━━━━━━━━━━━━━\n"
                "✅ Pump.fun WebSocket real-time\n"
                "✅ Bonding curve monitor\n"
                "✅ Holder distribution check\n"
                "✅ Exit signal detection\n"
                "✅ Boosted = penalty\n"
                "✅ GMGN exact label match\n"
                "✅ Resurrection validation\n\n"
                "/scan | /hold | /gmgn | /holders\n"
                "⏳ Starting..."
            ),
            parse_mode=ParseMode.MARKDOWN)

        await do_scan(bot, manual=False)

        tasks = [
            background_scanner(bot),
            background_wallet_poller(bot),
        ]
        if config.ENABLE_PUMPFUN_WS:
            tasks.append(pumpfun_websocket_listener())
            tasks.append(pumpfun_alert_processor(bot))

        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
