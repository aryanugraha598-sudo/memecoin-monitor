import os

# ─── TELEGRAM ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# ─── FILTER ──────────────────────────────────────────────────
MIN_LIQUIDITY_USD  = 15000
MIN_VOLUME_1H      = 500       # Diturunkan — pump.fun bisa explosive dari kecil

# ─── AGE SCORING (jam) ───────────────────────────────────────
AGE_TIER_FRESH     = 6
AGE_TIER_YOUNG     = 24
AGE_TIER_NORMAL    = 168
AGE_TIER_OLD       = 720

# ─── SCORE THRESHOLDS ────────────────────────────────────────
SCORE_MOONBAG = 75
SCORE_SWING   = 60
SCORE_SCALP   = 45

# ─── TIMING ──────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES   = 10
WALLET_POLL_INTERVAL_MIN = 3

# ─── PUMP.FUN WEBSOCKET ──────────────────────────────────────
ENABLE_PUMPFUN_WS        = True
# Min bonding curve % untuk alert (0-100)
# 80-95% = zona paling explosive sebelum graduate
PUMPFUN_BC_MIN_PCT       = 75
PUMPFUN_BC_ALERT_PCT     = 90    # Alert khusus kalau BC > 90%
# Min SOL terkumpul di bonding curve
PUMPFUN_MIN_SOL          = 50

# ─── FEATURES ────────────────────────────────────────────────
ENABLE_TWITTER_CHECK     = True
ENABLE_GMGN_SMART_MONEY  = True
ENABLE_HOLDER_CHECK      = True   # Solscan holder distribution
ENABLE_EXIT_MONITOR      = True   # Monitor sinyal exit untuk hold watchlist

# ─── HELIUS (opsional) ───────────────────────────────────────
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# ─── SMART MONEY WALLETS ─────────────────────────────────────
SMART_MONEY_WALLETS = [
    # Tambah wallet dari komunitas:
    # t.me/solanamemecoins, t.me/pumpfunalpha, gmgn.ai leaderboard
]

# ─── GMGN LABEL SCORES ───────────────────────────────────────
GMGN_LABEL_SCORES = {
    "insider":     25,
    "sniper":      18,
    "smart_degen": 20,
    "kol":         15,
    "whale":       12,
    "smart":       15,
}
GMGN_MAX_BONUS = 50

# ─── EXIT SIGNAL THRESHOLDS ──────────────────────────────────
EXIT_DUMP_PCT_1H    = -20    # Price drop 1h yang trigger exit warning
EXIT_VOL_COLLAPSE   = 0.3    # Vol accel turun ke 30% dari sebelumnya = exit warning
EXIT_BSR_DANGER     = 0.6    # Buy/sell ratio < 0.6 = distribusi berat
