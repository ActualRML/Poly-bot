# Polymarket Trading Bot

Bot ini adalah trading bot untuk Polymarket prediction market.

## Struktur
- `src/main.py` → entry point
- `src/logic/` → strategy, probability, kelly, dll
- `src/api/` → Polymarket + external API clients (Kalshi, Manifold)
- `src/utils/` → config, logger, telegram
- `src/backtest/` → backtester market making
- `script/backtest_mispricing.py` → backtest & kalibrasi model probabilitas
- `script/recalibrate.py` → auto-recalibration job
- `script/suggest_whitelist.py` → generator kandidat Polymarket↔Kalshi pair untuk review manual

## Rules
- Jangan modifikasi `.env*`
- Jangan commit API keys
- Selalu test sebelum push

---

## Status Sistem (update: 2026-04-27)

### Crypto Strategy (mispricing)

**Probability Model (`src/logic/probability.py`)**
- Rolling drift 30 hari per titik evaluasi
- `CALIBRATION_CORRECTION` dua tabel: `at_expiry` dan `barrier`
- Corrections di-apply otomatis di `_calculate()` — semua caller pakai model terkalibrasi
- Interpolasi linear untuk target_pct sembarang; correction = 0 kalau target < smallest point (default 3%)
- Concurrent IV fetch: lock cover seluruh fetch, 12 paralel call → 1 HTTP request

**Asset aktif: BTC, ETH, SOL** (XRP & DOGE excluded di `src/main.py`)

**Kalibrasi (recalibrate 2026-04-27, window 90d, regime bearish kuat):**

| Asset | at_expiry max | barrier max |
|---|---|---|
| BTC | -8% @ +8% target | -17% @ +8% |
| ETH | -10% @ +8% | -15% @ +8% |
| SOL | -9% @ +8% | -18% @ +5% |

**Catatan:** corrections mungkin over-aggressive karena window 90d include fase bearish ekstrem (BTC drift -73% annualized). Kalau bot jarang signal setelah paper trade 2 minggu → re-kalibrasi dengan window lebih panjang (180d atau 365d).

### Political Strategy (multi-source + whitelist)

**Sumber data aktif (paralel) di `src/api/metaculus_client.py`:**
- **Kalshi** — regulated US real money market, no auth, confidence 0.85
- **Manifold** — play money, confidence 0.60

**Flow di `political_mispricing.py`:**
1. `is_political_market(question)` filter — exclude crypto + sports keywords
2. `_is_whitelisted(condition_id)` check:
   - Whitelist kosong → permissive (warn sekali per session)
   - Whitelist ≥1 entry → strict (cuma condition_id terdaftar yang lanjut)
3. `metaculus_client.search_all()` fetch Kalshi + Manifold paralel
4. **Disagreement check** — kalau >1 sumber dan spread rate > 20%, skip
5. Return `list[BaseRate]` → weighted average by confidence
6. Gap > `POLITICAL_THRESHOLD` (0.08) → signal mispricing

**Whitelist (`data/political_whitelist.json`):**
- Format: `{"markets": {"<polymarket_condition_id>": "<kalshi_event_ticker>"}}`
- Status: 1 entry (Kevin Warsh Fed Chair) → strict mode aktif
- Generator: `python -m script.suggest_whitelist` → `data/whitelist_candidates.csv`

**Cache (in-memory, hilang saat restart):**
- Kalshi bulk events: 30 menit | Manifold per query: 30 menit | Final blended: 30 menit
- IV Deribit: 5 menit | CoinGecko crypto price: 5 menit

**Filter Kalshi:**
- Category: Politics, Elections, World, Climate and Weather, Science and Technology, Economics
- Skip provisional dan multivariate parlay (KXMVE*)
- Hanya market `status="active"` dengan price valid

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

### Dead/No-op
- **Fed base rate (`src/logic/fed_fetcher.py`)**: ambil Polymarket price sebagai base rate untuk market yang sama → gap selalu ~0. Disable di baris 154-157 `_get_base_rates` kalau mau hemat HTTP call.

### `script/recalibrate.py`
- Run backtest semua asset (BTC/ETH/SOL/XRP/DOGE) at_expiry + barrier, update `CALIBRATION_CORRECTION` di `probability.py`, notif Telegram
- **Wajib `PYTHONIOENCODING=utf-8`** di Windows (env override otomatis untuk subprocess, tapi prepend kalau jalan manual)
- Cron VPS: `0 2 1 * * cd /path/to/bot && PYTHONIOENCODING=utf-8 python -m script.recalibrate`
- **Status**: script siap, cron belum dipasang — tunggu VPS aktif

---

## Crypto Trade Simulator (2026-04-28)

End-to-end trade backtest, terpisah dari `main.py`. Simulasi full lifecycle entry→exit→resolve pakai data historis Polymarket + Deribit IV + CoinGecko spot.

**File baru (semua isolated, ga touch live system):**
- `script/backtest_crypto_trades.py` — CLI entry
- `src/backtest/polymarket_history.py` — closed markets via Gamma + price history via CLOB `/prices-history`
- `src/backtest/iv_history.py` — Deribit DVOL historis (BTC/ETH) + realized vol fallback (SOL)
- `src/backtest/question_parser.py` — strict filter (skip "X or Y first", multivariate, dll)
- `src/backtest/trade_simulator.py` — core simulator (reuse `probability.py` + `kelly.py` + `exit_strategy.py`)
- `data/backtest_cache/` — disk cache (re-run instant, TTL 24h–7d)
- `data/backtest_results/trades_*.csv` + `summary_*.txt`

**Hasil first-run 365d window:** 214 trades, ROI +12.81%, win rate 50.9%. **JANGAN trust angka ini sebelum validasi point #1 di Next Steps.**

**Run:**
```bash
PYTHONIOENCODING=utf-8 python -m script.backtest_crypto_trades --days 365
PYTHONIOENCODING=utf-8 python -m script.backtest_crypto_trades --days 90 --threshold 0.10
```

**Caveats:**
- CoinGecko free cap 365d → ga bisa langsung ke 5 tahun tanpa ganti spot source (Binance klines)
- Deribit DVOL cuma BTC + ETH; SOL pakai realized vol → mungkin under-estimate IV
- Banyak entry_prob = 0.999 (long-dated near-money) → model degenerate, sinyal valid tapi over-confident
- Re-entry instant after stop-loss (214 trades / 192 markets)

---

## Next Steps (prioritas — research dulu sebelum paper trade)

1. **Validasi backtest bukan bug** (BLOCKER sebelum trust angka ROI)
   - Spot-check 5–10 trades manual lawan Polymarket historical chart
   - Audit look-ahead bias: `fidelity=1440` (daily candle) → entry "hari T" pakai close-of-T, padahal live cuma punya open-of-T. Bias mungkin overstate ROI 1–3%
   - Cek resolution timing: market endDate scheduled vs actual close (banyak yang resolve early)

2. **Decompose source of edge** (kalau valid, masih perlu tau dari mana edge-nya)
   - Stratify by entry_prob bucket: 0.5–0.7, 0.7–0.9, 0.9+. Kalau profit cuma dari 0.9+ → strategy efektifnya "buy near-resolve YES at discount", bukan mispricing detection
   - Per-quarter breakdown ROI → cek apakah edge konsisten atau cuma 1 regime

3. **Baseline comparison** (tanpa baseline, +12.81% gak punya makna)
   - Naive long-YES (beli setiap market di entry, hold ke resolve) — ROI berapa?
   - Naive long-high-prob (beli kalau market_price > 0.7) — ROI?
   - Kalau strategy ≤ baseline → gap detection ga add value

4. **Decision point setelah point 1–3 clean:**
   - Path A: Extend ke 5 tahun (perlu Binance klines + handle sparse Polymarket data pre-2024)
   - Path B: Go live paper trade (skip extended backtest, real-money validation)

5. **Paper trade crypto 2 minggu** — defer sampai point 1–3 done
6. **Paper trade political 1 bulan** — defer
7. **Setup cron recalibrate di VPS** — setelah VPS aktif
8. **Go live** — setelah backtest validated + paper trade clean

---

## Roadmap: EXIT_EDGE_REVERSED Signal

Exit sekarang reaktif terhadap Polymarket price (trailing stop). Ide: exit proaktif jika probability model re-kalkulasi dengan harga terkini turun signifikan dari entry probability.

```
entry_prob   = 54%  (disimpan saat posisi dibuka)
current_prob = recalculate(harga BTC sekarang) = 15%
drop = 39% > threshold (20%) → EXIT_EDGE_REVERSED
```

**Yang perlu dibangun:**
1. Simpan `entry_prob` ke DB saat `open_position()` (schema migration)
2. Re-kalkulasi probability di exit evaluation
3. Tambah `EXIT_EDGE_REVERSED` signal di `src/logic/exit_strategy.py`
4. Config: `EDGE_REVERSAL_THRESHOLD=0.20`

**Kapan:** setelah paper trade selesai — evaluasi dulu apakah trailing stop sudah cukup.

---

## Cara Kalibrasi

**Otomatis (recommended):**
```bash
PYTHONIOENCODING=utf-8 python -m script.recalibrate
```

**Manual per asset:**
```bash
# at-expiry
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset BTC
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset ETH
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset SOL

# barrier
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90 --asset BTC
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90 --asset ETH
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --barrier --days 90 --asset SOL
```

Re-kalibrasi setiap **30–60 hari** atau saat regime berubah drastis. Ganti `--days 180` kalau 90d terlalu noisy.

---

## Cara Generate Whitelist Kandidat

```bash
python -m script.suggest_whitelist          # default 5 pages
python -m script.suggest_whitelist --pages 10  # coverage penuh
```

Output: `data/whitelist_candidates.csv` — sort by similarity desc, manual verify topik benar-benar sama, copy ke `data/political_whitelist.json` field `"markets"`.
