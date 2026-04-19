import os

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# Filter Settings
MIN_LIQUIDITY_USD = 30000        # Minimum liquidity $30k
MIN_HOLDER_COUNT = 200           # Minimum 200 holders
MAX_TOP10_CONCENTRATION = 45     # Max 45% held by top 10
MIN_VOLUME_24H = 50000           # Minimum volume 24h $50k
MIN_AGE_HOURS = 1                # Minimum age 1 jam
MAX_AGE_HOURS = 72               # Maximum age 72 jam (3 hari)

# Scoring Weights
WEIGHT_VOLUME_MOMENTUM = 30      # Volume momentum
WEIGHT_HOLDER_GROWTH = 25        # Pertumbuhan holder
WEIGHT_LIQUIDITY_RATIO = 25      # Rasio liquidity/mcap
WEIGHT_AGE_SWEET_SPOT = 20       # Age sweet spot

# Score Thresholds
SCORE_MOONBAG = 80               # Hold lama
SCORE_SWING = 65                 # Hold 1-7 hari
SCORE_SCALP = 50                 # Hold <2 jam

# Check interval (menit)
CHECK_INTERVAL_MINUTES = 15
