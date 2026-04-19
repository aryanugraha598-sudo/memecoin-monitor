import os

# ─── TELEGRAM ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# ─── FILTER ──────────────────────────────────────────────────
MIN_LIQUIDITY_USD  = 20000    # Min liquidity $20k
MAX_AGE_HOURS      = 120      # Max 5 hari (beri ruang resurrection)
MIN_AGE_HOURS      = 0        # Tidak ada minimum — fresh graduate dicari
MIN_VOLUME_24H     = 5000     # Min volume 24h (diturunkan untuk resurrection)

# ─── SCORE THRESHOLDS ────────────────────────────────────────
SCORE_MOONBAG = 75
SCORE_SWING   = 60
SCORE_SCALP   = 45

# ─── TIMING ──────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES = 10   # Scan setiap 10 menit

# ─── FEATURES ────────────────────────────────────────────────
ENABLE_TWITTER_CHECK = True
