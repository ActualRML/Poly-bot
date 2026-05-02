# Polymarket Trading Bot

Bot trading untuk Polymarket prediction market.

## Struktur
- `src/main.py` → entry point
- `src/logic/` → strategy, probability, kelly, dll
- `src/api/` → Polymarket + external API clients
- `src/utils/` → config, logger, telegram
- `src/backtest/` → simulator components (polymarket_history, iv_history, question_parser, trade_simulator)
- `script/backtest_mispricing.py` → backtest & kalibrasi model probabilitas
- `script/backtest_crypto_trades.py` → end-to-end crypto trade simulator
- `script/audit_backtest.py` → stratify + quarter + spot-check + resolution audit
- `script/baseline_backtest.py` → naive baselines (long YES / long YES>0.7 / long NO<0.3)
- `script/audit_lookahead.py` → quantify execution-shift bias
- `script/recalibrate.py` → auto-recalibration job

## Rules
- Jangan modifikasi `.env*`
- Jangan commit API keys
- Selalu test sebelum push

---

## Status Sistem (update: 2026-04-30)

### Crypto Strategy (mispricing + tiered entry)

**Probability Model (`src/logic/probability.py`)**
- Rolling drift 30 hari per titik evaluasi
- `CALIBRATION_CORRECTION` dua tabel: `at_expiry` dan `barrier`
- Corrections di-apply otomatis di `_calculate()` — semua caller pakai model terkalibrasi
- Interpolasi linear untuk target_pct sembarang; correction = 0 kalau target < smallest point (default 3%)

**Asset aktif: BTC, ETH, SOL, BNB** (XRP & DOGE excluded — MAE >4%, model tidak cocok)

**IV Sources:**
- BTC, ETH: Deribit DVOL
- SOL, BNB: CoinGecko realized vol 30d rolling (via `src/backtest/iv_history.py`)
- Live price: **Binance primary** (30s cache), CoinGecko fallback (5m cache)

**Kalibrasi terkini:**

| Asset | at_expiry MAE | barrier MAE | Kalibrasi |
|---|---|---|---|
| BTC | ~2-3% | ~3-4% | 2026-04-27, window 90d |
| ETH | ~2-3% | ~3-4% | 2026-04-27, window 90d |
| SOL | ~2-3% | ~3-4% | 2026-04-27, window 90d |
| BNB | **1.3%** | **2.0%** | 2026-04-28, window 365d |

**Catatan BTC/ETH/SOL:** corrections window 90d mungkin over-aggressive (regime bearish ekstrem). Re-kalibrasi dengan `--days 180` atau `--days 365` kalau signal jarang.

### Tiered Entry Strategy (aktif sejak 2026-04-30)

Dua tier entry berdasarkan gap dan winrate model:

| Tier | Gap (normal vol) | Gap (high vol) | Min Winrate | Kelly | Max kapital |
|---|---|---|---|---|---|
| T1 — Elite | ≥ 10% | ≥ 15% | 80% | 0.5x | 30% |
| T2 — Hustler | ≥ 5% | ≥ 8% | 65% | 0.25x | 5% |

**Dynamic volatility check:** BTC 24h realized vol (Binance) dikompute tiap cycle.
- Vol > 60% → regime HIGH, threshold naik ke T1=15%/T2=8%
- Vol ≤ 60% → regime NORMAL, threshold T1=10%/T2=5%

**Scan:** semua market crypto aktif, expiry 1–180 hari, volume > $10.000, polling 30 detik.

**Log per cycle:**
```
Vol regime: NORMAL | T1≥10% / T2≥5%
Daily scan: 126 market lolos filter
SIGNAL T1  Will BTC be above $90k... | BUY No @ 0.32 | Gap 12.1% | Winrate 83% | Kelly $8.40 (7.0%) | EV 0.14
SIGNAL T2  Will ETH be above $2000... | BUY Yes @ 0.41 | Gap 6.3% | Winrate 68% | Kelly $3.60 (3.0%) | EV 0.05
```

### Political Strategy

**Status: DISABLED** (`POLITICAL_ENABLED=False`). Belum dibacktest — aktifkan setelah validasi Kalshi calibration.

Kalau mau enable: set `POLITICAL_ENABLED=True` di `.env.local` dan isi `data/political_whitelist.json` dulu untuk kurangi false match.

### Lifecycle Posisi

**Buka:**
- `can_open()` cek seluruh market via `get_position_by_market()` (cegah beli YES+NO)
- `open_position()` simpan `gap_pct`, `kelly_fraction`, `strategy_mode`, `token_id` ke DB

**Update harga & exit:**
- `_backfill_missing_token_ids()` jalan tiap cycle — cari posisi tanpa token_id, fetch dari Gamma, simpan
- `_fetch_current_prices()` fetch best_bid dari CLOB pakai token_id, update DB
- `evaluate_exits()` apply trailing stop / lock profit / stale check

**Resolve & close:**
- `_resolve_checker()` jalan tiap cycle:
  - Settled normal: harga ≥0.98 atau ≤0.02 → auto-close
  - Force-close: >24 jam lewat resolve & masih mid-range → close di bid sekarang
  - Panggil `breaker.record_trade()` setelah close

### `script/recalibrate.py`
- Run backtest semua asset (BTC/ETH/SOL/BNB) at_expiry + barrier, update `CALIBRATION_CORRECTION` di `probability.py`, notif Telegram
- **Wajib `PYTHONIOENCODING=utf-8`** di Windows
- Cron VPS: `0 2 1 * * cd /path/to/bot && PYTHONIOENCODING=utf-8 python -m script.recalibrate`
- **Status**: script siap, cron belum dipasang — tunggu VPS aktif

---

## Crypto Trade Simulator (audited 2026-04-28)

**Files:**
- `script/backtest_crypto_trades.py` — CLI entry (default: lag=1, slippage=1%)
- `script/audit_backtest.py` — stratify, quarter, spot-check, resolution timing audit
- `script/baseline_backtest.py` — naive baselines (long YES, long YES>0.7, long NO<0.3)
- `script/audit_lookahead.py` — quantify execution-shift bias
- `data/backtest_cache/` — disk cache (TTL 24h–7d)
- `data/backtest_results/` — trades CSVs, summaries

**Hasil 365d (corrected, dengan slippage 1%):**

| Strategy | Trades | ROI |
|---|---|---|
| Main strategy (default) | 202 | +3.49% |
| Naive C (long NO<0.3) | 79 | +3.76% |

Strategy tied dengan naive baseline di threshold default lama (20%). Tiered entry (T1=10%, T2=5%) diharapkan meningkatkan frekuensi — butuh re-backtest untuk validasi.

**Run:**
```bash
PYTHONIOENCODING=utf-8 python -m script.backtest_crypto_trades --days 365
PYTHONIOENCODING=utf-8 python -m script.audit_backtest
PYTHONIOENCODING=utf-8 python -m script.baseline_backtest
```

---

## Next Steps

**Paper trade (AKTIF)** — `DRY_RUN=True`, tiered entry aktif, modal virtual $120.
- Monitor winrate setelah 20+ trade: kalau < 55% → naikkan threshold
- Stop criteria: ROI < -5% setelah 5+ trades → review config
- Asset aktif: BTC, ETH, SOL, BNB, expiry 1–180 hari

**Setelah paper trade:**
- Re-backtest dengan tiered threshold baru (T1=10%, T2=5%) untuk validasi edge
- Setup cron recalibrate di VPS
- Go live

**Roadmap: EXIT_EDGE_REVERSED Signal (defer)**

Exit sekarang reaktif terhadap Polymarket price (trailing stop). Ide: exit proaktif jika model probability re-kalkulasi turun signifikan dari entry.

```
entry_prob   = 54%
current_prob = recalculate(harga BTC sekarang) = 15%
drop = 39% > threshold (20%) → EXIT_EDGE_REVERSED
```

Yang perlu dibangun: simpan `entry_prob` ke DB, re-kalkulasi di exit evaluation, tambah signal baru di `exit_strategy.py`. Evaluasi dulu apakah trailing stop sudah cukup.

---

## Cara Kalibrasi

**Otomatis:**
```bash
PYTHONIOENCODING=utf-8 python -m script.recalibrate
```

**Manual per asset:**
```bash
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset BTC
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset ETH
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset SOL
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 365 --asset BNB

# barrier
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90 --asset BTC
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90 --asset ETH
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90 --asset SOL
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 365 --asset BNB
```

Re-kalibrasi setiap 30–60 hari atau saat regime berubah drastis.
