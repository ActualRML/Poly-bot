# Polymarket Trading Bot

Bot trading untuk Polymarket prediction market.
**Tiga strategy aktif: Crypto Daily + Up/Down Daily + Up/Down Hourly.**

## Struktur
- `src/main.py` → entry point + main loop async (semua strategy jalan di sini)
- `src/logic/` → strategy, probability, kelly, risk_manager, circuit_breaker, exit_strategy, mispricing, manager, updown_strategy
- `src/api/` → Polymarket CLOB, Gamma, Binance clients
- `src/utils/` → config, logger, telegram_alert
- `src/models/` → database (SQLite), types
- `script/backtest_mispricing.py` → backtest & kalibrasi model probabilitas (Daily strategy)
- `script/backtest_updown.py` → backtest kalibrasi Up/Down Daily
- `script/recalibrate.py` → auto-recalibration job (BTC/ETH/SOL/BNB)
- `script/monitor.py` → monitor posisi live

## Config
Dua file env (gitignored):
- `.env.secret` → credentials (PK, CLOB_*, Telegram tokens)
- `.env.local`  → strategy params (Kelly, risk, threshold, dll)
- `.env.example` → template referensi (committed, aman) — **copas bagian .env.local ke .env.local**

Endpoint defaults sudah ada di `config.py`. **Jangan commit env files.**

## Rules
- Jangan modifikasi `.env*` tanpa konfirmasi
- Jangan commit API keys
- Selalu test sebelum push

---

## Strategy 1: Crypto Daily

Market "Will BTC be above $X?" yang resolve dalam 5 menit – 24 jam ke depan (`HOURLY_MAX_MINUTES_TO_RESOLVE=1440`).
Bot scalping via `MIN_PROFIT_PCT=0.20` — masuk hanya kalau upside ≥ 20%, exit via profit lock di tengah.

### Probability Model (`src/logic/probability.py`)
- Log-normal + barrier crossing model
- `CALIBRATION_CORRECTION` dua tabel: `at_expiry` dan `barrier`
- Correction di-apply di kedua arah (above & below) — pakai `abs(target_pct)`, simetris
- Interpolasi linear; correction = 0 kalau target < smallest point (default 3%)

**Asset aktif: BTC, ETH, SOL, BNB** (XRP & DOGE excluded — MAE >4%)

**IV Sources:**
- Live realized vol: **Binance** 4h rolling (lower bound 2%)
- Live price: Binance primary (30s cache), CoinGecko fallback (5m cache)

**Kalibrasi terkini:**

| Asset | at_expiry MAE | barrier MAE | Window | Tanggal |
|---|---|---|---|---|
| BTC | ~2-3% | ~3-4% | 90d | 2026-04-27 |
| ETH | ~2-3% | ~3-4% | 90d | 2026-04-27 |
| SOL | ~2-3% | ~3-4% | 90d | 2026-04-27 |
| BNB | **1.3%** | **2.0%** | 365d | 2026-04-28 |

### Dynamic Threshold (`src/logic/strategy.py`)

```
threshold = vol_annual / sqrt(24) * 1.5   →  clamped [6%, 25%]
```

`should_force_exit(expiry_time)` — trigger jual < 10 menit sebelum expiry.

### Market Filter (`src/api/gamma_client.py`)

`ascan_hourly_opportunities` filter urutan:
1. Status — skip `closed/active=false/archived/resolved`
2. Order book — skip `enableOrderBook=false`
3. Kategori — skip sports/entertainment/music/awards/tv/movies/gaming
4. Volume — skip < `HOURLY_MIN_MARKET_VOLUME` ($5000)
5. Liquidity — skip < `HOURLY_MIN_LIQUIDITY` ($200)
6. Time window — skip di luar `[HOURLY_MIN_MINUTES_TO_RESOLVE, HOURLY_MAX_MINUTES_TO_RESOLVE]`

### `script/recalibrate.py`
- Run backtest BTC/ETH/SOL/BNB → update `CALIBRATION_CORRECTION` di `probability.py` → notif Telegram
- Per-asset window: BTC/ETH/SOL = 90d, BNB = 365d (`DAYS_BY_ASSET` dict)
- Cron VPS: `0 2 1 * * cd /path/to/bot && python -m script.recalibrate`
- **Status**: script siap, cron belum dipasang

---

## Strategy 2: Up/Down Daily

### Status: **LIVE — paper trade aktif di `main.py`**

Market "Bitcoin Up or Down on May 4?" — resolve sekali sehari jam 16:00 UTC.

### Series IDs Gamma (untuk `/events?series_id=X`)
| Asset | Series ID |
|---|---|
| BTC | 41 |
| ETH | 40 |
| SOL | 10086 |
| XRP | 10100 |

**BNB tidak punya daily Up/Down series di Polymarket.**

### Reference Price
`fetch_reference_price()` → Binance 1-min close 16:00 UTC kemarin.

### Hasil Backtest (90 hari, n=44 per asset)

| Asset | Accuracy | Bias |
|---|---|---|
| BTC | **90.9%** | -0.016 (OK) |
| ETH | **84.1%** | -0.032 (slight) |
| SOL | **75.0%** | -0.066 (under-conf) |
| XRP | **84.1%** | -0.004 (OK) |

### Config
- `UPDOWN_THRESHOLD` (default 0.05) — min edge 5%
- `UPDOWN_MAX_HOURS` (default 8.0) — skip entry kalau expiry > 8 jam lagi
- `strategy_mode` DB: `updown_dry_run` (paper) / `updown` (live)

---

## Strategy 3: Up/Down Hourly

### Status: **LIVE — paper trade aktif di `main.py`**

Market "Bitcoin Up or Down - May 6, 1AM ET" — resolve setiap jam.

### Scan Logic
Query `/events` dengan filter `end_date_min`/`end_date_max` (window: 5–`UPDOWN_HOURLY_MAX_MINUTES` menit).
5m markets (`btc-updown-5m-...`) dan 15m markets (`btc-updown-15m-...`) di-skip via `_UPDOWN_HOURLY_SKIP_MARKERS`.

**Asset aktif: BTC, ETH, SOL, XRP, DOGE, BNB** (HYPE diexclude — tidak ada di Binance)

### Reference Price
`fetch_reference_price_hourly()` → open 1h Binance candle di `market_start_date`.

### Hasil Backtest (30 hari, entry T-30m, n=719 per asset)

| Asset | Accuracy | Bias |
|---|---|---|
| BTC | **75.1%** | -0.033 |
| ETH | **74.4%** | -0.025 |
| SOL | **74.8%** | -0.015 |
| XRP | **73.0%** | -0.020 |
| DOGE | **72.9%** | -0.020 |
| BNB | **76.6%** | -0.018 |

Edge ≥ 5% → hanya **14% candles**, accuracy **90.3%** dalam backtest. **Paper trade actual** (n=25): edge bukan predictor reliable (avg edge wins 13.6% ≈ avg edge losses 13.6%) — filter momentum dan trailing diperlukan.

### Config
- `UPDOWN_HOURLY_THRESHOLD=0.07` — min edge 7% (naik dari 0.05 setelah audit)
- `UPDOWN_HOURLY_MAX_MINUTES=90` — window scan
- `UPDOWN_HOURLY_MAX_ENTRY_PRICE=0.65` — skip entry kalau buy_price > 0.65 (upside terlalu tipis)
- `UPDOWN_HOURLY_MOMENTUM_MINUTES=15`, `UPDOWN_HOURLY_MOMENTUM_THRESHOLD=0.003` — skip Up signal kalau asset turun >0.3% in 15m
- `strategy_mode` DB: `updown_hourly_dry_run` (paper) / `updown_hourly` (live)

---

## Exit Strategy (`src/logic/exit_strategy.py`)

### Profit Lock per Strategy

| Strategy | Trigger 1 | Trigger 2 |
|---|---|---|
| Daily Crypto | PnL ≥ 20% & > 60m left | PnL ≥ 35% & > 30m left |
| Up/Down Daily | PnL ≥ 40% & > 60m left | PnL ≥ 60% & > 30m left |
| Up/Down Hourly | PnL ≥ 30% & > 30m left | PnL ≥ 50% & > 20m left |
| Up/Down Hourly trailing | Peak PnL ≥ 15% + retrace ≥ 30% from peak (sisa > 10m) | — |
| Near-expiry (≤ 20m) | Hold to resolve | — |

Config: `PROFIT_LOCK_PCT`, `PROFIT_LOCK_HIGH_PCT`, `UPDOWN_PROFIT_LOCK_PCT`, `UPDOWN_PROFIT_LOCK_HIGH_PCT`, `HOURLY_PROFIT_LOCK_PCT`, `HOURLY_PROFIT_LOCK_HIGH_PCT`, `HOURLY_TRAILING_ACTIVATE_PCT`, `HOURLY_TRAILING_RETRACE_PCT`

### No Re-entry Setelah Profit Lock
`_profit_locked_markets` — set session-level di `main.py`. Market yang sudah di-profit-lock tidak akan di-enter lagi sampai bot di-restart.

### Trailing Stop (Dynamic)
```
stop = P * (1-P) * 2.0 × vol_scale   →  clamped [5%, 45%]
```
Update setiap cycle berdasarkan rata-rata harga posisi open dan BTC vol.

---

## Risk Manager (`src/logic/risk_manager.py`)

**Adaptive position size** (cap terhadap Kelly):
- 2+ consecutive losses → cap **$10**
- 3+ consecutive wins → cap **$30**
- Default → **$20**

**Direction cap:** `MAX_SAME_DIRECTION=0` (disabled untuk paper trade). Set `2-3` saat go live.

**Slot cap:** `MAX_POSITIONS_PER_SLOT=0` (disabled untuk paper trade). Crypto correlation positif kuat — saat winrate tinggi (≥70%), filter ini lebih sering blokir cluster wins daripada cegah cluster losses. Set `3` saat go live untuk cegah skenario ekstrem 4-5 posisi searah.

---

## Circuit Breaker (`src/logic/circuit_breaker.py`)

**PnL-based** (`check()`):
- Saklar 1: daily loss > 20% modal (`MAX_DAILY_LOSS_PCT`) → pause sampai besok (auto reset)
- Saklar 2: 5x consecutive loss (`MAX_CONSECUTIVE_LOSSES`) → pause (**manual reset**)
- Saklar 3: drawdown > 20% (`MAX_DRAWDOWN_PCT`) → emergency stop (**manual reset**)

**Market-condition** (`check_safety_thresholds()`):
- BTC realized vol > 100% annualized → halt entry baru
- Daily drawdown < -15% → halt entry baru

State: `data/circuit_breaker.json`.

**Manual reset setelah Saklar 2/3:**
Edit `data/circuit_breaker.json` — set `saklar_2_triggered`/`saklar_3_triggered` ke `false`,
reset `consecutive_losses` ke 0, `daily_loss` ke 0, update `starting_capital` ke modal sekarang.

**HANYA blokir entry baru.** Exit posisi tetap jalan walau CB aktif.

---

## Lifecycle Posisi

**Per-cycle (urutan dalam main loop):**

```
EXIT BLOCK — selalu jalan, tidak diblokir CB:
  1. _backfill_missing_token_ids
  2. _resolve_checker  (hanya posisi expire_date < now)
  3. balance display + summary (DRY_RUN: SALDO_AWAL + realized_pnl - locked)
  4. _build_vol_data (Binance realized vol BTC/ETH/SOL/BNB)
  5. _fetch_current_prices (CLOB best_bid pakai token_id)
  6. dynamic trailing stop update
  7. _force_exit_check (< 10 menit sebelum expiry)
  8. evaluate_exits (trailing stop / profit lock)
  9. update _profit_locked_markets dari exits

ENTRY BLOCK — hanya kalau CB & safety OK:
  10. CB check → continue kalau triggered
  11. _prefetch_prices (cache crypto prices)
  12. ascan_hourly_opportunities → _analyze_market × N      [Daily Crypto]
  13. _scan_updown_markets → _analyze_updown_market × 4     [Up/Down Daily]
  14. _scan_updown_hourly_markets → _analyze_updown_hourly_market × N  [Up/Down Hourly]
```

---

## Paper Trade

**Status: AKTIF** — `DRY_RUN=True`, modal awal $120, **compound otomatis**.

Balance dry run = `SALDO_AWAL + realized_pnl - locked_capital` → profit ikut compound ke bet berikutnya.

**Stop criteria:**
- Winrate < 55% setelah 20+ trade → naikkan threshold
- ROI < -5% setelah 10+ trade → review config

---

## Minimalisir Latency (saat Go Live)

1. **VPS US-East** (AWS us-east-1, Vultr NJ) — cut latency 5–10x vs lokal Indonesia
2. **Parallel API calls** — fetch harga + order book pakai `asyncio.gather()`
3. **Persistent HTTP session** — `aiohttp.ClientSession` dibuat sekali, di-reuse
4. **Kurangi mid-path fetch** — semua data harus ready sebelum order dikirim

---

## Audit Hourly (2026-05-07) — semua done

Audit dari 25 trade paper menemukan: edge bukan predictor reliable (avg edge wins 13.6% ≈ losses 13.6%) dan bot tidak panen profit di tengah (15/17 wins tunggu resolve). Solusi yang sudah implementasi:

1. ✅ Turunkan profit lock hourly 60→30 / 60→50
2. ✅ Momentum filter — skip Up signal kalau asset turun >0.3% in 15m
3. ✅ Threshold edge naik 0.05→0.07
4. ✅ Max entry price hourly 0.65 (skip kalau upside < 35%)
5. ✅ Trailing profit lock — activate peak PnL ≥15%, exit kalau retrace ≥30%

Validasi: butuh 20-30 trade baru untuk konfirmasi target winrate ≥75%.

---

## Next Steps

| Priority | Task |
|---|---|
| 🔴 | Pantau 20-30 trade hourly post-audit, validasi winrate naik dan exit_lock_profit dominan |
| 🟡 | Setup cron recalibrate di VPS |
| 🟢 | Go live setelah paper trade terbukti edge |

---

## Test Coverage

**Run semua tests:**
```bash
pytest tests/ -q --tb=short
```

### 260 tests — semua passing, 0 warnings

| File | Test File |
|---|---|
| `src/logic/kelly.py` | `tests/test_kelly.py` |
| `src/logic/risk_manager.py` | `tests/test_risk_manager.py` |
| `src/logic/probability.py` | `tests/test_probability.py` |
| `src/logic/exit_strategy.py` | `tests/test_exit_strategy.py` |
| `src/logic/mispricing.py` | `tests/test_mispricing.py` |
| `src/logic/circuit_breaker.py` | `tests/test_circuit_breaker.py` |
| `src/logic/pricing.py` | `tests/test_pricing.py` |
| `src/logic/updown_strategy.py` | `tests/test_updown_strategy.py` |
| `src/logic/strategy.py` | `tests/test_strategy.py` |
| `src/api/binance_client.py` | `tests/test_async_binance.py` |
| `src/api/gamma_client.py` | `tests/test_gamma_filter.py` |
| `src/api/clob_client.py` | `tests/test_clob_client.py` |
| `src/models/database.py` | `tests/test_database.py` |
| `src/logic/manager.py` | `tests/test_manager.py` |
| `src/utils/config.py` | `tests/test_config.py` |

---

## Cara Kalibrasi Daily Strategy

**Otomatis:**
```bash
python -m script.recalibrate
```

**Manual per asset:**
```bash
python -m script.backtest_mispricing --days 90  --asset BTC
python -m script.backtest_mispricing --days 90  --asset ETH
python -m script.backtest_mispricing --days 90  --asset SOL
python -m script.backtest_mispricing --days 365 --asset BNB
```

Re-kalibrasi setiap 30–60 hari atau saat regime berubah drastis.
