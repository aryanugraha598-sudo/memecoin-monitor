import os

# ─── TELEGRAM ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# ─── FILTER ──────────────────────────────────────────────────
MIN_LIQUIDITY_USD  = 20000    # Min liquidity $20k
MAX_AGE_HOURS      = 120      # Max 5 hari (beri ruang resurrection)
MIN_AGE_HOURS      = 0
MIN_VOLUME_24H     = 5000

# ─── SCORE THRESHOLDS ────────────────────────────────────────
SCORE_MOONBAG = 75
SCORE_SWING   = 60
SCORE_SCALP   = 45

# ─── TIMING ──────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES    = 10   # Scan token setiap 10 menit
WALLET_POLL_INTERVAL_MIN  = 3    # Cek aktivitas SM wallet setiap 3 menit

# ─── FEATURES ────────────────────────────────────────────────
ENABLE_TWITTER_CHECK      = True
ENABLE_SMART_MONEY_CHECK  = True

# ─── HELIUS API ──────────────────────────────────────────────
# Daftar gratis: https://dev.helius.xyz/
# Free tier 100k credits/bulan — cukup untuk polling rutin
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# ─── SMART MONEY WALLET LIST ─────────────────────────────────
# Tambahkan wallet profitable yang kamu riset dari:
# - GMGN.ai leaderboard (gmgn.ai/sol/address)
# - Nansen top traders (nansen.ai/solana-onchain-data)
# - Dune: "Solana Alpha Wallets" dashboard
# - Solscan: cari wallet dengan win rate tinggi
#
# Bisa juga tambah via bot: /wallet add <address>
SMART_MONEY_WALLETS = [
    # Isi address di sini, contoh format:
    # "H72yLkhTnoBfhBTXXaj1RBXuirm8s8G5fcVh2XpQLggM",
]

# Bonus score per SM wallet yang ketahuan early buy
SMART_MONEY_SCORE_BONUS = 15   # per wallet
SMART_MONEY_MAX_BONUS   = 45   # cap total bonus (max 3 wallet dihitung)
