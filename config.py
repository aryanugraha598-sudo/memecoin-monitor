import os

# ─── TELEGRAM ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# ─── FILTER ──────────────────────────────────────────────────
MIN_LIQUIDITY_USD  = 15000
MIN_VOLUME_1H      = 500

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
ENABLE_PUMPFUN_WS    = True

# PENTING: Jangan turunkan di bawah 85
# BC 85%+ = token sudah hampir graduate, ada masa kritis
# BC 95%+ = hampir pasti graduate dalam menit ke depan
PUMPFUN_BC_MIN_PCT   = 85    # Alert pertama saat BC mencapai ini
PUMPFUN_BC_ALERT_PCT = 95    # Alert khusus URGENT saat BC mencapai ini

# Min SOL di bonding curve (filter token yang benar-benar bergerak)
PUMPFUN_MIN_SOL      = 70    # ~82% dari 85 SOL target graduation

# ─── FEATURES ────────────────────────────────────────────────
ENABLE_TWITTER_CHECK     = True
ENABLE_GMGN_SMART_MONEY  = True
ENABLE_HOLDER_CHECK      = True
ENABLE_EXIT_MONITOR      = True

# ─── HELIUS (opsional) ───────────────────────────────────────
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# ─── SMART MONEY WALLETS ─────────────────────────────────────
SMART_MONEY_WALLETS = [
    # Tambah dari gmgn.ai/leaderboard atau /wallet add <addr>
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
EXIT_DUMP_PCT_1H   = -20
EXIT_VOL_COLLAPSE  = 0.3
EXIT_BSR_DANGER    = 0.6
