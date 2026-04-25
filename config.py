import os

# ─── TELEGRAM ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6008408532")

# ─── FILTER DASAR ────────────────────────────────────────────
MIN_LIQUIDITY_USD  = 15000
MIN_VOLUME_1H      = 500

# ─── RUG FILTER (LAPISAN 1 — HARD REJECT) ────────────────────
# Bundler: jika top holders beli di blok sama (bundled launch)
BUNDLER_MAX_PCT        = 8      # bundler % dari supply → reject kalau lebih
# Top holder concentration
TOP3_HOLDER_MAX_PCT    = 55     # top 3 wallet pegang > ini → reject
TOP10_HOLDER_MAX_PCT   = 75     # top 10 wallet pegang > ini → reject
# Dev holding
DEV_HOLD_MAX_PCT       = 10     # dev pegang > ini → curiga, kurangi score
DEV_HOLD_REJECT_PCT    = 20     # dev pegang > ini → hard reject
# Wash trading
WASH_HARD_REJECT       = 3      # wash level 3 → langsung reject (sudah ada, sekarang enforced)
# Liquidity vs MCap ratio minimum
LIQ_MCAP_MIN_RATIO     = 0.03   # liq harus minimal 3% dari mcap

# ─── SMART MONEY FILTER (LAPISAN 2) ──────────────────────────
# Minimum smart money score untuk boost sinyal
SM_BOOST_MIN           = 15     # SM bonus >= ini → tingkatkan priority alert
# Fresh wallet whale — umur wallet dalam hari
FRESH_WALLET_AGE_DAYS  = 7      # wallet < 7 hari = fresh wallet (suspicious kecuali SM)

# ─── DEAD COIN REVIVAL SCANNER (LAPISAN 3) ───────────────────
# Deteksi koin lama yang tiba-tiba hidup kembali
REVIVAL_MIN_TOKEN_AGE_H    = 48     # token harus berumur minimal 48 jam
REVIVAL_DORMANT_DAYS       = 2      # min hari dengan volume rendah sebelumnya
REVIVAL_VOL_SPIKE_MULT     = 8      # volume 1h harus 8x dari rata-rata 7 hari
REVIVAL_MIN_VOL_1H         = 5000   # volume 1h minimal $5000 untuk trigger revival
REVIVAL_LP_INCREASE        = True   # LP harus bertambah (bukan berkurang) untuk valid
REVIVAL_MIN_HOLDER_GROWTH  = 10     # holder baru per jam minimal 10 untuk revival valid

# ─── SCORING WEIGHTS (LAPISAN 4) ─────────────────────────────
# Weighted scoring — bukan flat addition
SCORE_WEIGHT_SM            = 1.5    # multiplier untuk SM signal
SCORE_WEIGHT_REVIVAL       = 1.3    # multiplier untuk resurrection signal
SCORE_PENALTY_BOOSTED      = 15     # penalty untuk boosted token
SCORE_PENALTY_WASH2        = 15     # penalty wash level 2
SCORE_PENALTY_HOLDER_CONC  = 20     # penalty holder terkonsentrasi (tidak di-reject)
SCORE_PENALTY_OLD_TOKEN    = 35     # penalty token sangat tua (>720 jam)

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
AW_STATUS_HOLD     = 45
AW_STATUS_WEAK     = 25

# ─── PUMP ALERT ──────────────────────────────────────────────
AW_PUMP_ALERT_PCT  = 50
AW_MOON_ALERT_PCT  = 200

# ─── TIMING ──────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES   = 10
WALLET_POLL_INTERVAL_MIN = 3
AUTO_WL_CHECK_EVERY      = 15
AUTO_WL_MAX_AGE_H        = 72
AUTO_WL_MAX              = 50
AUTO_WL_SCORE_MIN        = 45

# ─── RESURRECTION TUNING ─────────────────────────────────────
RESURRECTION_AVG_THRESHOLD = 300
RESURRECTION_VOL_SPIKE     = 3000
RESURRECTION_MIN_MULT      = 5

# ─── DEAD COIN SCAN — endpoint tambahan ──────────────────────
# Bot v12 scan koin tua yang revival via DexScreener trending + gainers
ENABLE_DEAD_COIN_SCAN      = True
DEAD_COIN_SCAN_LIMIT       = 30     # berapa koin tua yang di-scan per cycle
DEAD_COIN_MIN_AGE_H        = 48     # minimal umur token untuk masuk dead coin scan
DEAD_COIN_VOL_MULTIPLIER   = 5      # vol 1h harus 5x dari vol average untuk trigger

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
ENABLE_RUG_FILTER        = True     # v12: hard rug filter sebelum scoring
ENABLE_REVIVAL_SCAN      = True     # v12: dead coin revival scanner

# ─── HELIUS ──────────────────────────────────────────────────
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# ─── SMART MONEY WALLETS ─────────────────────────────────────
SMART_MONEY_WALLETS = []

# ─── GMGN LABEL SCORES ───────────────────────────────────────
GMGN_LABEL_SCORES = {
    "insider":     25,
    "sniper":      18,
    "smart_degen": 20,
    "kol":         15,
    "whale":       12,
    "smart":       15,
    "early_buyer": 10,
}
GMGN_MAX_BONUS = 50

# ─── EXIT SIGNAL THRESHOLDS ──────────────────────────────────
EXIT_DUMP_PCT_1H   = -20
EXIT_VOL_COLLAPSE  = 0.3
EXIT_BSR_DANGER    = 0.6

# ─── RUG SCORE SYSTEM (v12) ──────────────────────────────────
# Skor risiko 0-100, makin tinggi makin berbahaya
# Koin dengan rug_score >= threshold ini di-reject
RUG_SCORE_REJECT   = 60    # rug score >= 60 → hard reject
RUG_SCORE_WARN     = 35    # rug score >= 35 → warning di alert
