# Polymarket Trading Bot

Bot trading untuk Polymarket prediction market. **Crypto Hourly Strategy only.**

## Struktur
- `src/main.py` → entry point + main loop async
- `src/logic/` → strategy, probability, kelly, risk_manager, circuit_breaker, exit_strategy, mispricing, manager
- `src/api/` → Polymarket CLOB, Gamma, Binance clients
- `src/utils/` → config, logger, telegram_alert
- `src/backtest/` → simulator components (polymarket_history, iv_history, question_parser, trade_simulator)
- `src/models/` → database (SQLite), types
- `script/backtest_mispricing.py` → backtest & kalibrasi model probabilitas
- `script/backtest_hourly_trades.py` → end-to-end hourly trade simulator
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

## Status Sistem (update: 2026-05-03, session 2)

**Audit lengkap selesai** — 11 issue diidentifikasi & difix. Code aman untuk paper trade & live trading.

### Crypto Hourly Strategy

**Probability Model (`src/logic/probability.py`)**
- Log-normal + barrier crossing model
- `CALIBRATION_CORRECTION` dua tabel: `at_expiry` dan `barrier`
- Correction di-apply di kedua arah (above & below) — pakai `abs(target_pct)`, simetris
- Interpolasi linear; correction = 0 kalau target < smallest point (default 3%)
- `_barrier_prob` punya overflow guard untuk vol kecil + target jauh

**Asset aktif: BTC, ETH, SOL, BNB** (XRP & DOGE excluded — MAE >4%)

**IV Sources:**
- Live realized vol: **Binance** 4h rolling (lower bound 2% — tidak reject signal di malam tenang)
- Historical IV backtest: `src/backtest/iv_history.py`
- Live price: Binance primary (30s cache), CoinGecko fallback (5m cache)

**Kalibrasi terkini:**

| Asset | at_expiry MAE | barrier MAE | Window | Tanggal |
|---|---|---|---|---|
| BTC | ~2-3% | ~3-4% | 90d | 2026-04-27 |
| ETH | ~2-3% | ~3-4% | 90d | 2026-04-27 |
| SOL | ~2-3% | ~3-4% | 90d | 2026-04-27 |
| BNB | **1.3%** | **2.0%** | 365d | 2026-04-28 |

---

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

---

### Risk Manager (`src/logic/risk_manager.py`)

**Dynamic trailing stop**:
```
stop = P * (1-P) * 2.0 × vol_scale   →  clamped [5%, 45%]
```
P mendekati 0/1 → stop ketat. P~0.5 → stop lebar.

**Adaptive position size** (cap terhadap Kelly):
- 2+ consecutive losses → cap **$10**
- 3+ consecutive wins → cap **$30**
- Default → **$20**

**Catatan**: `MAX_CAPITAL_PER_MARKET=30%` di env memungkinkan cap $30 saat hot streak (sebelumnya 20% → bug, $30 selalu di-reject).

---

### Circuit Breaker (`src/logic/circuit_breaker.py`)

**PnL-based** (`check()`):
- Saklar 1: daily loss > 10% modal → pause sampai besok
- Saklar 2: 3x consecutive loss → pause (manual reset)
- Saklar 3: drawdown > 20% → emergency stop (manual reset)

**Market-condition** (`check_safety_thresholds()`):
- BTC realized vol > 100% annualized → halt entry baru
- Daily drawdown < -15% → halt entry baru

State: `data/circuit_breaker.json`. Audit log: `data/safety_halt.log`.

**HANYA blokir entry baru.** Exit posisi tetap jalan walau CB aktif.

---

### Lifecycle Posisi

**Buka:**
- `can_open()` cek seluruh market via `get_position_by_market()` (cegah beli YES+NO)
- `open_position()` simpan `gap_pct`, `kelly_fraction`, `strategy_mode`, `token_id` ke DB
- **`_open_position_lock`** mengwrap `can_open` → `open_position` (cegah race di `asyncio.gather`)

**Startup (sekali sebelum loop):**

```
STARTUP — jalankan sekali saat bot pertama start:
  1. _backfill_missing_token_ids
  2. reconcile_positions — sync posisi open vs Gamma API
     - Cek SEMUA posisi open (bukan hanya yang expired)
     - Tangkap early resolve & posisi stuck dari crash sebelumnya
     - Fallback CLOB: close kalau best_bid ≥0.98 atau ≤0.02
     - Timeout per posisi di-handle, bot tidak crash
```

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
  11. ascan_hourly_opportunities
  12. safety check (vol > 100% atau drawdown < -15%)
  13. _analyze_market × N (asyncio.gather, lock per entry)
```

**Resolve checker vs reconcile_positions:**

| | `_resolve_checker` | `reconcile_positions` |
|---|---|---|
| Kapan jalan | Setiap cycle | Sekali saat startup |
| Syarat posisi | `resolve_date < now` | Semua posisi open |
| Tangkap early resolve | ❌ | ✅ |

**Resolve checker:**
- Harga ≥0.98 atau ≤0.02 → auto-close (resolved)
- >24 jam lewat resolve & harga mid-range → force close di bid sekarang

**Order book kosong:**
- Muncul saat market near-resolved tapi Gamma belum `closed=True`
- Bot tetap jalan — exit eval pakai harga terakhir dari DB
- `reconcile_positions` saat restart akan catch posisi ini via CLOB fallback

---

### Market Filter (`src/api/gamma_client.py`)

`ascan_hourly_opportunities` filter urutan:
1. Status — skip `closed/active=false/archived/resolved`
2. Order book — skip `enableOrderBook=false`
3. Kategori — skip sports/entertainment/music/awards/tv/movies/gaming
4. Volume — skip < `HOURLY_MIN_MARKET_VOLUME` ($500)
5. Liquidity — skip < `HOURLY_MIN_LIQUIDITY` ($200)
6. Time window — skip di luar `[HOURLY_MIN_MINUTES_TO_RESOLVE, HOURLY_MAX_MINUTES_TO_RESOLVE]`

`get_token_prices` & `extract_token_ids` adalah `@staticmethod` — tidak butuh client instance.

---

### Mispricing Detector (`src/logic/mispricing.py`)

`analyze_market(analyze_yes_only=True)` default — hanya analisis Yes side. Caller derive No-side decision dari direction (UNDERPRICED Yes ↔ buy Yes, OVERPRICED Yes ↔ buy No). Hemat 50% compute.

Pass `analyze_yes_only=False` di backtest yang butuh raw No-side numbers.

---

### `script/recalibrate.py`
- Run backtest BTC/ETH/SOL/BNB → update `CALIBRATION_CORRECTION` di `probability.py` → notif Telegram
- Per-asset window: BTC/ETH/SOL = 90d, BNB = 365d (`DAYS_BY_ASSET` dict)
- **Wajib `PYTHONIOENCODING=utf-8`** di Windows
- Cron VPS: `0 2 1 * * cd /path/to/bot && PYTHONIOENCODING=utf-8 python -m script.recalibrate`
- **Status**: script siap, cron belum dipasang

---

## Paper Trade

**Status: AKTIF** — `DRY_RUN=True`, modal virtual $120.

Log per cycle:
```
[VOL] BTC 45% | ETH 42% | SOL 68% | BNB 33% (annualized)
Hourly scan: 9/64 lolos | skip: status=31 ...
SIGNAL  Will BTC be above $90k... | BUY No @ 0.32 | Gap 13.8% | Winrate 83% | Kelly $8.40 (7.0%) | EV 0.14
```

**Stop criteria:**
- Winrate < 55% setelah 20+ trade → naikkan threshold multiplier ke 2.0
- ROI < -5% setelah 5+ trade → review config, stop paper trade

---

## Next Steps

1. **Monitor paper trade** — kumpulkan 20+ trade, cek winrate & ROI
2. **Re-backtest** dengan dynamic threshold (sebelumnya backtest pakai threshold 20% statis)
3. **Setup cron recalibrate** di VPS
4. **Go live** setelah paper trade menunjukkan edge nyata

**Roadmap: EXIT_EDGE_REVERSED (defer)**
Exit proaktif kalau model probability turun signifikan dari saat entry. Butuh: simpan `entry_prob` ke DB, re-kalkulasi di exit evaluation. Evaluasi dulu apakah trailing stop sudah cukup.

---

## Cara Kalibrasi

**Otomatis:**
```bash
PYTHONIOENCODING=utf-8 python -m script.recalibrate
```

**Manual per asset:**
```bash
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90  --asset BTC
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90  --asset ETH
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90  --asset SOL
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 365 --asset BNB

# barrier
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90  --asset BTC
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90  --asset ETH
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90  --asset SOL
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 365 --asset BNB
```

Re-kalibrasi setiap 30–60 hari atau saat regime berubah drastis.
