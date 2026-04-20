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

# ─── AUTO WATCHLIST STATUS THRESHOLDS ────────────────────────
# FIX: threshold lebih ketat agar status lebih akurat
AW_STATUS_HOLD     = 45   # score >= ini → 🟢 HOLD
AW_STATUS_WEAK     = 25   # score >= ini → 🟡 LEMAH
# score < AW_STATUS_WEAK → 🔴 EXIT

# FIX: Pump threshold untuk alert take profit
AW_PUMP_ALERT_PCT  = 50   # alert kalau MCap naik >= 50% dari entry
AW_MOON_ALERT_PCT  = 200  # alert khusus kalau 3x+

# ─── TIMING ──────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES   = 10
WALLET_POLL_INTERVAL_MIN = 3
AUTO_WL_CHECK_EVERY      = 15   # cek auto watchlist setiap 15 menit
AUTO_WL_MAX_AGE_H        = 72   # hapus dari AW setelah 72 jam
AUTO_WL_MAX              = 50
AUTO_WL_SCORE_MIN        = 45

# ─── RESURRECTION TUNING ─────────────────────────────────────
# FIX: Buat resurrection lebih sensitif untuk koin seperti MISA
# (MCap terjun ke 5k lalu recovery ke 45k)
RESURRECTION_AVG_THRESHOLD = 300   # avg vol baseline (default 200, naikkan sedikit)
RESURRECTION_VOL_SPIKE     = 3000  # vol 1h harus > ini untuk trigger resurrection
RESURRECTION_MIN_MULT      = 5     # min multiplier vol vs baseline

# ─── PUMP.FUN WEBSOCKET ──────────────────────────────────────
ENABLE_PUMPFUN_WS    = True
PUMPFUN_BC_MIN_PCT   = 85
PUMPFUN_BC_ALERT_PCT = 95
PUMPFUN_MIN_SOL      = 70

# ─── FEATURES ────────────────────────────────────────────────
ENABLE_TWITTER_CHECK     = True
ENABLE_GMGN_SMART_MONEY  = True
ENABLE_HOLDER_CHECK      = True
ENABLE_EXIT_MONITOR      = True

# ─── HELIUS (opsional) ───────────────────────────────────────
# Daftar gratis: https://dev.helius.xyz/
# Kalau tidak ada, SM wallet polling tidak aktif
# tapi GMGN auto smart money tetap jalan
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# ─── SMART MONEY WALLETS (manual, via /wallet add) ──────────
# GMGN auto smart money sudah jalan tanpa ini.
# List ini untuk real-time polling via Helius.
# Cara isi: ketik /wallet add <address> di Telegram
SMART_MONEY_WALLETS = []

# ─── GMGN LABEL SCORES ───────────────────────────────────────
GMGN_LABEL_SCORES = {
    "insider":     25,
    "sniper":      18,
    "smart_degen": 20,
    "kol":         15,
    "whale":       12,
    "smart":       15,
    "early_buyer": 10,   # tambahan: label GMGN early buyer
}
GMGN_MAX_BONUS = 50

# ─── EXIT SIGNAL THRESHOLDS ──────────────────────────────────
EXIT_DUMP_PCT_1H   = -20   # dump > 20% dalam 1 jam → exit warning
EXIT_VOL_COLLAPSE  = 0.3   # volume drop ke < 30% dari sebelumnya
EXIT_BSR_DANGER    = 0.6   # BSR < 0.6 → distribusi berat
