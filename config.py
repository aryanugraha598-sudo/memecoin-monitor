import os

# ─── TELEGRAM ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# ─── FILTER ──────────────────────────────────────────────────
MIN_LIQUIDITY_USD  = 20000    # Min liquidity $20k
MIN_VOLUME_1H      = 1000     # Min vol 1h $1k

# ─── AGE SCORING ─────────────────────────────────────────────
# Tidak ada hard limit umur lagi — semua koin bisa lolos.
# Umur hanya mempengaruhi SCORE (koin tua perlu sinyal lebih kuat).
# Tier umur (jam):
AGE_TIER_FRESH      = 6      # < 6 jam  → bonus fresh graduate
AGE_TIER_YOUNG      = 24     # < 24 jam → bonus muda
AGE_TIER_NORMAL     = 168    # < 7 hari → normal
AGE_TIER_OLD        = 720    # < 30 hari → sedikit penalty
# >= 720 jam (30 hari+) → perlu resurrection kuat untuk lolos

# ─── SCORE THRESHOLDS ────────────────────────────────────────
SCORE_MOONBAG = 75
SCORE_SWING   = 60
SCORE_SCALP   = 45

# ─── TIMING ──────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES    = 10   # Scan token setiap 10 menit
WALLET_POLL_INTERVAL_MIN  = 3    # Poll SM wallet setiap 3 menit

# ─── FEATURES ────────────────────────────────────────────────
ENABLE_TWITTER_CHECK     = True
ENABLE_GMGN_SMART_MONEY  = True   # Cek top traders via GMGN public API (gratis, tanpa key)

# ─── HELIUS API (opsional, untuk wallet activity polling) ────
# Daftar gratis: https://dev.helius.xyz/
# Kalau kosong, fitur /wallet check tetap jalan tapi tanpa Helius
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# ─── SMART MONEY WALLET LIST (manual, opsional) ─────────────
# GMGN akan otomatis detect smart money per token.
# Daftar ini untuk wallet yang mau kamu poll aktivitasnya secara real-time.
# Cari dari: gmgn.ai leaderboard, nansen.ai, dune.com
SMART_MONEY_WALLETS = [
    # "H72yLkhTnoBfhBTXXaj1RBXuirm8s8G5fcVh2XpQLggM",
]

# Bonus score per SM wallet / label GMGN yang ditemukan
GMGN_LABEL_SCORES = {
    "smart_degen": 20,   # label GMGN: Smart Degen
    "kol":         15,   # label GMGN: KOL (Key Opinion Leader)
    "sniper":      18,   # label GMGN: Sniper
    "insider":     25,   # label GMGN: Insider — tertinggi
    "whale":       12,   # label GMGN: Whale
    "smart":       15,   # generic "smart money"
}
GMGN_MAX_BONUS = 50     # cap total bonus dari GMGN
