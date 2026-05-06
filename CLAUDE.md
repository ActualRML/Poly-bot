# Polymarket Trading Bot

Bot trading untuk Polymarket prediction market.
**Tiga strategy aktif: Crypto Daily + Up/Down Daily + Up/Down Hourly.**

## Struktur
- `src/main.py` → entry point + main loop async (semua strategy jalan di sini)
- `src/logic/` → strategy, probability, kelly, risk_manager, circuit_breaker, exit_strategy, mispricing, manager, updown_strategy
- `src/api/` → Polymarket CLOB, Gamma, Binance clients
- `src/utils/` → config, logger, telegram_alert
- `src/backtest/` → (kosong — simulator lama sudah dihapus)
- `src/models/` → database (SQLite), types
- `script/backtest_mispricing.py` → backtest & kalibrasi model probabilitas (Daily strategy)
- `script/backtest_updown.py` → backtest kalibrasi Up/Down Daily
- `script/recalibrate.py` → auto-recalibration job (BTC/ETH/SOL/BNB)
- `script/monitor.py` → monitor posisi live

## Config
Dua file env (gitignored):
- `.env.secret` → credentials (PK, CLOB_*, Telegram tokens)
- `.env.local`  → strategy params (Kelly, risk, threshold, dll)
- `.env.example` → template referensi (committed, aman)

Endpoint defaults sudah ada di `config.py`. **Jangan commit env files.**

## Rules
- Jangan modifikasi `.env*` tanpa konfirmasi
- Jangan commit API keys
- Selalu test sebelum push

---

## Strategy 1: Crypto Daily

Market "Will BTC be above $X?" yang resolve dalam 5–90 menit ke depan.

### Probability Model (`src/logic/probability.py`)
- Log-normal + barrier crossing model
- `CALIBRATION_CORRECTION` dua tabel: `at_expiry` dan `barrier`
- Correction di-apply di kedua arah (above & below) — pakai `abs(target_pct)`, simetris
- Interpolasi linear; correction = 0 kalau target < smallest point (default 3%)

**Asset aktif: BTC, ETH, SOL, BNB** (XRP & DOGE excluded — MAE >4%)

**IV Sources:**
- Live realized vol: **Binance** 4h rolling (lower bound 2%)
- Historical IV backtest: `src/backtest/iv_history.py`
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

| Vol (annual) | Threshold |
|---|---|
| 20% | 6.1% (floor) |
| 40% (normal) | ~12.2% |
| 70% (tinggi) | ~21.4% |
| 100%+ | 25% (cap) |

`should_force_exit(expiry_time)` — trigger jual < 10 menit sebelum expiry.

### Market Filter (`src/api/gamma_client.py`)

`ascan_hourly_opportunities` filter urutan:
1. Status — skip `closed/active=false/archived/resolved`
2. Order book — skip `enableOrderBook=false`
3. Kategori — skip sports/entertainment/music/awards/tv/movies/gaming
4. Volume — skip < `HOURLY_MIN_MARKET_VOLUME` ($500)
5. Liquidity — skip < `HOURLY_MIN_LIQUIDITY` ($200)
6. Time window — skip di luar `[HOURLY_MIN_MINUTES_TO_RESOLVE, HOURLY_MAX_MINUTES_TO_RESOLVE]`

### Mispricing Detector (`src/logic/mispricing.py`)

`analyze_market(analyze_yes_only=True)` default — hanya analisis Yes side. Caller derive No-side decision dari direction (UNDERPRICED Yes ↔ buy Yes, OVERPRICED Yes ↔ buy No). Hemat 50% compute.

### `script/recalibrate.py`
- Run backtest BTC/ETH/SOL/BNB → update `CALIBRATION_CORRECTION` di `probability.py` → notif Telegram
- Per-asset window: BTC/ETH/SOL = 90d, BNB = 365d (`DAYS_BY_ASSET` dict)
- Cron VPS: `0 2 1 * * cd /path/to/bot && python -m script.recalibrate`
- **Status**: script siap, cron belum dipasang

---

## Strategy 2: Up/Down Daily

### Status: **LIVE — paper trade aktif di `main.py`**

Market "Bitcoin Up or Down on May 4?" — resolve sekali sehari jam 16:00 UTC.
Berjalan berbarengan dengan strategy lain dalam satu loop.

### File
- `src/logic/updown_strategy.py` — `calculate_updown_probability()`, `fetch_reference_price()`
- `script/backtest_updown.py` — backtest historis via Gamma series endpoint

### Series IDs Gamma (untuk `/events?series_id=X`)
| Asset | Series ID | Ticker |
|---|---|---|
| BTC | 41 | btc-up-or-down-daily |
| ETH | 40 | eth-up-or-down-daily |
| SOL | 10086 | solana-up-or-down-daily |
| XRP | 10100 | xrp-up-or-down-daily |

**BNB tidak punya daily Up/Down series di Polymarket.**

### Reference Price
- **Live** (active market): `fetch_reference_price()` → Binance 1-min close 16:00 UTC kemarin
- **Backtest** (resolved market): `eventMetadata.priceToBeat` dari Gamma — sudah tersedia langsung

### Probability Model
Log-normal at-expiry: `P(S_T >= reference) = Φ(d2)`, drift=0 risk-neutral.
Implementasi di `src/logic/updown_strategy.py:calculate_updown_probability()`.

Signal kuat saat T = 2–8 jam dan harga sudah jauh bergerak dari reference.
**Skip entry kalau `time_left > UPDOWN_MAX_HOURS` (default 8h)** — signal di T > 8h terlalu noise.

### Hasil Backtest (90 hari, n=44 per asset)
Entry simulasi: 90 menit sebelum expiry.

| Asset | Accuracy | Bias | n |
|---|---|---|---|
| BTC | **90.9%** | -0.016 (OK) | 44 |
| ETH | **84.1%** | -0.032 (slight) | 44 |
| SOL | **75.0%** | -0.066 (under-conf) | 44 |
| XRP | **84.1%** | -0.004 (OK) | 44 |

MAE ~43% normal untuk near-50/50 market — gunakan Accuracy sebagai metrik.

### Config
- `UPDOWN_THRESHOLD` (default 0.05) — min edge 5%
- `UPDOWN_MAX_HOURS` (default 8.0) — skip entry kalau expiry > 8 jam lagi
- `strategy_mode` DB: `updown_dry_run` (paper) / `updown` (live)

---

## Strategy 3: Up/Down Hourly

### Status: **LIVE — paper trade aktif di `main.py`**

Market "Bitcoin Up or Down - May 6, 1AM ET" — resolve setiap jam.
Berjalan berbarengan dengan strategy lain dalam satu loop.

### File
- `src/logic/updown_strategy.py` — `calculate_updown_probability_hourly()`, `fetch_reference_price_hourly()`
- `src/main.py` — `_scan_updown_hourly_markets()`, `_analyze_updown_hourly_market()`

### Slug Format Gamma
Format: `{asset}-up-or-down-{month}-{day}-{year}-{hour}am/pm-et`

Contoh: `bitcoin-up-or-down-may-6-2026-1am-et`

**Asset aktif: BTC, ETH, SOL, XRP, DOGE, BNB** (HYPE diexclude — tidak ada di Binance)

### Scan Logic
Query `/events` dengan filter `end_date_min`/`end_date_max` (window: 5–`UPDOWN_HOURLY_MAX_MINUTES` menit).
**Penting:** tanpa date filter, Gamma mengembalikan ribuan 5m markets pre-created yang menutupi hourly markets.

5m markets (`btc-updown-5m-...`) dan 15m markets (`btc-updown-15m-...`) di-skip via `_UPDOWN_HOURLY_SKIP_MARKERS`.

### Reference Price
`fetch_reference_price_hourly()` → open 1h Binance candle di `market_start_date` (bukan 16:00 UTC kemarin seperti Daily).

### Probability Model
Log-normal at-expiry identik dengan Daily, tapi T dalam hitungan menit bukan jam.
Implementasi di `src/logic/updown_strategy.py:calculate_updown_probability_hourly()`.

### Hasil Backtest (30 hari, entry T-30m, n=719 per asset)
`python -m script.backtest_updown_hourly --days 30 --entry_min 30`

| Asset | Accuracy | Bias | n |
|---|---|---|---|
| BTC | **75.1%** | -0.033 (under-conf) | 719 |
| ETH | **74.4%** | -0.025 (OK) | 719 |
| SOL | **74.8%** | -0.015 (OK) | 719 |
| XRP | **73.0%** | -0.020 (OK) | 719 |
| DOGE | **72.9%** | -0.020 (OK) | 719 |
| BNB | **76.6%** | -0.018 (OK) | 719 |

**Distribusi edge (semua asset, 4314 candles):**
- Edge ≥ 5% → hanya **14% candles**, accuracy **90.3%** ← signal yang valid
- Edge ≥ 10% → hanya **1% candles**, accuracy **85%**

**Kesimpulan:** Ada edge nyata tapi sangat selektif. Model benar 90% saat yakin (edge ≥ 5%), tapi mayoritas waktu model bilang ~50% → tidak ada signal → **benar tidak entry**. `UPDOWN_HOURLY_THRESHOLD=0.05` sudah tepat.

### Config
- `UPDOWN_HOURLY_THRESHOLD` (default 0.05) — min edge 5%
- `UPDOWN_HOURLY_MAX_MINUTES` (default 90) — window scan, **terpisah** dari `HOURLY_MAX_MINUTES_TO_RESOLVE` milik Daily Crypto (1440)
- `strategy_mode` DB: `updown_hourly_dry_run` (paper) / `updown_hourly` (live)

---

## Risk Manager (`src/logic/risk_manager.py`)

**Dynamic trailing stop**:
```
stop = P * (1-P) * 2.0 × vol_scale   →  clamped [5%, 45%]
```
P mendekati 0/1 → stop ketat. P~0.5 → stop lebar.

**Adaptive position size** (cap terhadap Kelly):
- 2+ consecutive losses → cap **$10**
- 3+ consecutive wins → cap **$30**
- Default → **$20**

`MAX_CAPITAL_PER_MARKET=30%` di env memungkinkan cap $30 saat hot streak.

---

## Circuit Breaker (`src/logic/circuit_breaker.py`)

**PnL-based** (`check()`):
- Saklar 1: daily loss > 10% modal → pause sampai besok (auto reset)
- Saklar 2: 3x consecutive loss → pause (**manual reset**)
- Saklar 3: drawdown > 20% → emergency stop (**manual reset**)

**Market-condition** (`check_safety_thresholds()`):
- BTC realized vol > 100% annualized → halt entry baru
- Daily drawdown < -15% → halt entry baru

State: `data/circuit_breaker.json`. Audit log: `data/safety_halt.log`.

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
  3. balance display + summary
  4. _build_vol_data (Binance realized vol BTC/ETH/SOL/BNB)
  5. _fetch_current_prices (CLOB best_bid pakai token_id)
  6. dynamic trailing stop update
  7. _force_exit_check (< 10 menit sebelum expiry)
  8. evaluate_exits (trailing stop / lock profit)

ENTRY BLOCK — hanya kalau CB & safety OK:
  9. CB check → continue kalau triggered
  10. _prefetch_prices (cache crypto prices)
  11. ascan_hourly_opportunities → _analyze_market × N      [Daily Crypto]
  12. _scan_updown_markets → _analyze_updown_market × 4     [Up/Down Daily]
  13. _scan_updown_hourly_markets → _analyze_updown_hourly_market × N  [Up/Down Hourly]
```

**Startup (sekali sebelum loop):**
```
  1. _backfill_missing_token_ids
  2. reconcile_positions — sync posisi open vs Gamma + CLOB
```

**Resolve checker vs reconcile_positions:**

| | `_resolve_checker` | `reconcile_positions` |
|---|---|---|
| Kapan jalan | Setiap cycle | Sekali saat startup |
| Syarat posisi | `resolve_date < now` | Semua posisi open |
| Tangkap early resolve | ❌ | ✅ |

---

## Paper Trade

**Status: AKTIF** — `DRY_RUN=True`, modal virtual $120.

**Stop criteria:**
- Winrate < 55% setelah 20+ trade → naikkan threshold
- ROI < -5% setelah 10+ trade → review config

---

## Minimalisir Latency (saat Go Live)

**1. VPS dekat server Polymarket (terbesar)**
- Bot lokal di Indonesia: round-trip ke Polymarket US ~200–400ms per request
- VPS US-East (AWS us-east-1, Vultr New Jersey, dll): turun ke ~20–50ms
- Ini sendiri cut latency 5–10x, lakukan ini sebelum optimasi lain

**2. Parallel API calls di path kritis**
- Fetch harga + fetch order book harus `asyncio.gather()`, bukan sequential await
- Cek di `main.py`: pastikan tidak ada await satu-satu yang bisa diparallelkan

**3. Persistent HTTP session**
- `aiohttp.ClientSession` dibuat sekali di startup, di-reuse selama bot jalan
- Bukan dibuat baru per request (ada overhead TCP handshake tiap kali)

**4. Kurangi langkah antara signal → order**
- Path ideal: detect signal → validasi edge → send order
- Semua data (harga, token_id) harus sudah tersedia dari prefetch sebelumnya
- Tidak ada fetch tambahan di antara signal dan order placement

| Langkah | Effort | Impact |
|---|---|---|
| VPS US-East | Medium | Sangat tinggi |
| Parallel API calls | Low | Tinggi |
| Persistent HTTP session | Low | Medium |
| Reduce mid-path fetch | Medium | Medium |

---

## Next Steps

| Priority | Task |
|---|---|
| 🔴 | Kumpulkan 20+ trade paper per strategy, cek winrate & ROI |
| 🟡 | Setup cron recalibrate di VPS |
| 🟢 | Go live setelah paper trade terbukti edge |

---

## Test Coverage

**Run semua tests:**
```bash
pytest tests/ -v
pytest tests/ -q --tb=short
```

### Sudah ditest (233 tests) — semua passing, 0 warnings

| File | Test File | Keterangan |
|---|---|---|
| `src/logic/kelly.py` | `tests/test_kelly.py` | Hypothesis — bet bounds, EV check |
| `src/logic/risk_manager.py` | `tests/test_risk_manager.py` | Hypothesis — stop loss [5%,45%], sizing [$10,$30] |
| `src/logic/probability.py` | `tests/test_probability.py` | Hypothesis — norm_cdf, barrier, expiry |
| `src/logic/exit_strategy.py` | `tests/test_exit_strategy.py` | Hypothesis — tidak crash, signal valid |
| `src/logic/mispricing.py` | `tests/test_mispricing.py` | Hypothesis — blend, direction, invert |
| `src/logic/circuit_breaker.py` | `tests/test_circuit_breaker.py` | Saklar 1/2/3, safety thresholds |
| `src/logic/pricing.py` | `tests/test_pricing.py` | Hypothesis — tick bounds, PnL sign |
| `src/logic/updown_strategy.py` | `tests/test_updown_strategy.py` | norm_cdf symmetry, monotone; `detect_updown_market` semua symbol + edge cases |
| `src/logic/strategy.py` | `tests/test_strategy.py` | `get_dynamic_threshold` clamp [6%,25%]; `should_force_exit` timezone |
| `src/api/binance_client.py` | `tests/test_async_binance.py` | Concurrent lock, rate limit, partial failure |
| `src/api/gamma_client.py` | `tests/test_gamma_filter.py` | `_filter_markets` skip logic; `get_token_prices`; `extract_token_ids` |
| `src/api/clob_client.py` | `tests/test_clob_client.py` | DRY_RUN path; `get_balance` fallback; `ambil_snapshot` parse |
| `src/models/database.py` | `tests/test_database.py` | Temp SQLite; UPSERT; migration; PnL aggregate |
| `src/logic/manager.py` | `tests/test_manager.py` | `can_open` limits; `_row_to_position` datetime; `get_unrealized_pnl` |
| `src/utils/config.py` | `tests/test_config.py` | `_get_bool` variants; `_get_float/_int/_decimal` |

`tests/conftest.py` — `gc.collect()` autouse fixture (cegah ResourceWarning sqlite3 di Python 3.12+)

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

# barrier
python -m script.backtest_mispricing --barrier --days 90  --asset BTC
python -m script.backtest_mispricing --barrier --days 90  --asset ETH
python -m script.backtest_mispricing --barrier --days 90  --asset SOL
python -m script.backtest_mispricing --barrier --days 365 --asset BNB
```

Re-kalibrasi setiap 30–60 hari atau saat regime berubah drastis.
