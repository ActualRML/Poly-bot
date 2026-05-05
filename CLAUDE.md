# Polymarket Trading Bot

Bot trading untuk Polymarket prediction market.
**Dua strategy aktif: Crypto Daily + Up/Down Daily. Up/Down Hourly dikerjakan terpisah.**

## Struktur
- `src/main.py` → entry point + main loop async (kedua strategy jalan di sini)
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
- `.env.secret` → credentials (PK, CLOB_*, FRED, Telegram tokens)
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
Berjalan berbarengan dengan Daily strategy dalam satu loop.

### File
- `src/logic/updown_strategy.py` — `calculate_updown_probability()`, `fetch_reference_price()`
- `script/backtest_updown.py` — backtest historis via Gamma series endpoint

### Tipe Market Up/Down di Polymarket
| Tipe | Contoh | Reference | Source | Status |
|---|---|---|---|---|
| **Daily** | "Bitcoin Up or Down on May 3?" | Binance 1-min close 16:00 UTC kemarin | Binance | ✅ Live |
| **Hourly** | "Bitcoin Up or Down - May 3, 1PM ET" | Open 1h Binance candle saat itu | Binance | 🔧 TODO |
| Chainlink | "Bitcoin Up or Down - May 3, 4:30PM-4:35PM ET" | Harga 5/15-min window | Chainlink | ⏭ skip |

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
Saat baru buka (T ~22 jam), model ≈ 0.5 → edge kecil → normal tidak ada signal.

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
- `UPDOWN_THRESHOLD` (default 0.05) — min edge 5% di `.env.local`
- `strategy_mode` DB: `updown_dry_run` (paper) / `updown` (live)

---

## Strategy 3: Up/Down Hourly — TODO (dikerjakan terpisah)

**Owner: teman**

Market "Bitcoin Up or Down - May 4, 1PM ET" — resolve setiap jam.

### Yang perlu dikerjakan:
1. **Reference price** — open 1h Binance candle saat market buka (beda dari Daily yang pakai 16:00 UTC kemarin)
2. **Backtest** — script baru atau extend `script/backtest_updown.py` dengan flag `--type hourly`
3. **Series IDs** untuk Hourly belum diketahui — perlu dicari di Gamma
4. **Integrasi ke `main.py`** — tambah `_scan_updown_hourly_markets()` dan `_analyze_updown_hourly_market()` (ikuti pola Daily)
5. **Threshold** — kemungkinan butuh nilai berbeda dari Daily karena T lebih pendek

### Catatan arsitektur:
- Hourly Up/Down markets bisa masuk time window `ascan_hourly_opportunities` (5–90 menit), tapi reference price-nya beda → **jangan reuse** `_get_base_rates()`
- Buat fungsi scan tersendiri seperti Daily, jangan tercampur dengan Crypto Daily scanner
- `strategy_mode` DB: gunakan `updown_hourly_dry_run` / `updown_hourly`

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
  11. ascan_hourly_opportunities → _analyze_market × N  [Daily strategy]
  12. _scan_updown_markets → _analyze_updown_market × 4  [Up/Down Daily]
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

Log per cycle:
```
Daily crypto + Up/Down Daily strategy aktif (async).
[VOL] BTC 57% | ETH 68% | SOL 60% | BNB 60% (annualized)
Daily scan: 36 market lolos filter
[UPDOWN] 4 active Up/Down Daily markets
[UPDOWN] BTC 6.2h left | BUY Up @ 0.420 | P(Up)=0.631 Mkt=0.420 Edge=+0.211 | Kelly $10.00
```

**Stop criteria:**
- Winrate < 55% setelah 20+ trade → naikkan threshold
- ROI < -5% setelah 10+ trade → review config

---

## Next Steps

| Priority | Task | Owner |
|---|---|---|
| 🔴 | Kumpulkan 20+ trade paper, cek winrate & ROI | Monitor |
| 🟡 | Setup cron recalibrate di VPS | - |
| 🟡 | Up/Down Hourly — backtest + integrasi | Teman |
| 🟢 | Go live setelah paper trade terbukti edge | - |

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
