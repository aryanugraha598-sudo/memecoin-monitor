import os

# ─── TELEGRAM ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# ─── FILTER SETTINGS ─────────────────────────────────────────
MIN_LIQUIDITY_USD = 20000        # Minimum liquidity $20k
MIN_VOLUME_1H = 1000             # Minimum volume 1h $1k (ada aktivitas SEKARANG)
MAX_AGE_HOURS = 120              # Max 5 hari (beri ruang resurrection)

# Tidak ada MIN_AGE — fresh graduate justru kita cari
MIN_AGE_HOURS = 0

# ─── SCORE THRESHOLDS ────────────────────────────────────────
SCORE_MOONBAG = 75               # Potensi hold lama
SCORE_SWING = 60                 # Hold 1-7 hari
SCORE_SCALP = 45                 # Hold <2 jam

# ─── TIMING ──────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES = 10      # Scan setiap 10 menit (lebih sering)

# ─── FEATURES ────────────────────────────────────────────────
ENABLE_TWITTER_CHECK = True      # Twitter/Nitter mention check
MIN_VOLUME_24H = 10000           # Minimum volume 24h (diturunkan agar resurrection bisa masuk)
