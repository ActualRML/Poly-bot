# Polymarket Trading Bot

@STRATEGY_MISTAKES.md

## Token Saving (CRITICAL)

- **Zero filler**: Jangan "I understand", "Based on the code", "Let me know if..." — langsung action.
- **Short confirm**: Cukup "Done" / "Fixed" untuk task admin.
- **Terminal truncation**: Selalu pipe `| Select-Object -Last 50` (PowerShell) atau `| tail -n 50` (bash).
- **Diff only**: Edit file → snippet perubahan saja, jangan tulis ulang seluruh file.
- **Test errors**: Tampilkan Traceback + Error Message terakhir saja.
- **No re-read**: Jangan baca ulang file yang sudah ada di chat history.

---

## Struktur

```
src/main.py               → entry point + main loop (semua strategy)
src/logic/                → strategy, probability, kelly, risk_manager, circuit_breaker,
                            exit_strategy, mispricing, manager, updown_strategy,
                            gbm_hourly, regime_filter, reentry, oracle_arb
src/api/                  → CLOB, Gamma, Binance clients
src/utils/                → config, logger, telegram_alert
src/models/               → database (SQLite), types
script/recalibrate.py     → auto-recalibration BTC/ETH/SOL/BNB
script/monitor.py         → monitor posisi (stdout)
script/monitor_bot.py     → Telegram bot interaktif (/status, /positions, /trades, /stats)
```

## Config Files

- `.env.secret` → credentials (gitignored)
- `.env.local`  → strategy params (gitignored)
- `.env.example` → template (committed) — **jangan commit env files**

---

## Strategies (semua paper trade, DRY_RUN=True, modal $120)

### 1. Crypto Daily
Market "Will BTC be above $X?" resolve 5m–24h. Asset: BTC, ETH, SOL, BNB.
Model: log-normal + barrier. Threshold dynamic: `vol/sqrt(24)*1.5` clamped [6%, 25%].
Recalibrate: `python -m script.recalibrate` (setiap 30–60 hari).

### 2. Up/Down Daily
Market "BTC Up or Down?" resolve 16:00 UTC. Asset: BTC (41), ETH (40), SOL (10086), XRP (10100).
Ref price: Binance 1m close 16:00 UTC kemarin. Min edge: `UPDOWN_THRESHOLD=0.05`.

### 3. Up/Down Hourly — GBM PROBABILITY (directional fair-value)
Market "BTC Up or Down - 1AM ET?" resolve tiap jam. Asset: BTC, ETH, SOL, XRP, DOGE, BNB.
Skip: 5m dan 15m markets.

**Direction logic — `src/logic/gbm_hourly.py`**:
- Strike = Binance 1h candle open di start_date (cached per market)
- `P(Up) = gbm_prob_above(current, strike, vol_annual, T_remaining)` (closed-form GBM)
- `edge_up   = P(Up)     - market_price_up   - fee`
- `edge_down = (1-P(Up)) - market_price_down - fee`
- Pick side dengan edge ≥ `adj_min_edge`, else SKIP
  `adj_min_edge = max(UPDOWN_HOURLY_GBM_MIN_EDGE, vol_annual × UPDOWN_GBM_VOL_EDGE_FACTOR)`
  e.g. DOGE (100%) → 10%, BNB (56%) → 5.6%, BTC (44%) → 4.4%
- Toggle `UPDOWN_HOURLY_USE_GBM=false` → fallback ke contrarian lama

**Filter stack** (tiap entry harus lolos semua):
1. Slot cap: `MAX_POSITIONS_PER_SLOT=2` open + `_HOURLY_MAX_ENTRIES_PER_SLOT=3` cumulative
2. Per-symbol blacklist: 3 loss berturut-turut → pause symbol 4 jam (active, wired di evaluate_exits + resolve_checker)
3. Candle open delay: skip 5m awal candle (`UPDOWN_HOURLY_CANDLE_OPEN_MIN`)
4. Volume ratio ≥ `UPDOWN_HOURLY_MIN_VOL_RATIO` (default 0.5, vs baseline 30m)
5. Outcome price stagnation: skip kalau Polymarket price <0.5% range dalam 5m
6. Min/max entry: `0.20 ≤ buy_price ≤ 0.45`
7. Scalping signal gate (BTC): skip `WAIT_NOISE` / `WAIT_TREND` / km=0
8. Market regime filter (`regime_filter.py`):
   - Cross-asset: ≥70% asset searah → +2
   - HTF 1h+4h alignment → +1
   - Session bias (US_OPEN) → +1
   - Score ≥ 4 → SKIP (only when `USE_GBM=false`; GBM mode bypass — rides trend)
9. Liquidity check pre-entry (CLOB orderbook)
10. **GBM mode bypasses momentum-window gating** (filter redundant — edge gate sudah handle)

**`gap_pct` recorded di DB**: GBM mode → realized edge (e.g. 0.08 = 8%); contrarian mode → BTC 15m momentum (legacy).

**Sizing**:
- `buy_winrate = clamp(GBM prob_up, 0.50, 0.80)` — sisi yang dibeli (Up→prob_up, Down→1−prob_up); fallback 0.55 kalau GBM disabled
- Kelly bet × `kelly_multiplier` (0.5/0.75/1.0 dari ATR vs ATR_avg; ×1.2 mom aligned, ×0.75 mom opposed)
- Asia session: cap kelly_multiplier ke 0.7

---

## Exit Strategy untuk Hourly (`src/logic/exit_strategy.py`)

**Asimetris: profit lock cepat, SL hanya di akhir.**

### Profit Lock (lock cepat, ride sisanya)
| Tier | PnL trigger | Time gate |
|---|---|---|
| T1 | ≥ 200% | > 5m left (near-max ITM) |
| T2 | ≥ 150% | > 15m left (substantial profit) |

Selain itu → HOLD ke resolve untuk full payout.

**Exit alerts**: setiap exit (TP maupun SL) kirim `alert_exit` ke Telegram + `log.info [EXIT]` ke terminal.
Format log: `[EXIT] ✅/❌ {SIGNAL} — {question} | {outcome} @ entry→exit | PnL $X`

### Late-Stage SL (exclusive bands, threshold makin lenient dekat resolve)
| Band | Time range | Threshold |
|---|---|---|
| OUTER | 10–20m left | PnL ≤ −30% |
| MIDDLE | 5–10m left | PnL ≤ −50% |
| INNER | 0–5m left | PnL ≤ −70% |

> 20m left → NO SL (kasih ruang recovery, filosofi hold to resolve).

---

## Re-entry After Take-Profit

### Same-direction re-entry (`src/logic/reentry.py`)

Setelah TP fire, candidate registered. Setiap cycle scan:

1. Drop ≥ 30% dari exit_price
2. Fair value (`estimate_fair_value`) > current_price + fee + 5% edge
3. Orderbook: spread ≤ 5%, liquidity cukup
4. Time gate: ≥ 15m to resolve
5. Slot cumulative cap belum penuh

→ Re-entry @ **half size** dari original capital.

### Opposite-direction re-entry (`gbm_hourly.passes_opposite_reentry_gate`)

Capture fakeout reversal — saat TP fire lalu harga reverse balik:

1. `UPDOWN_HOURLY_OPPOSITE_REENTRY=true` aktif
2. Time floor: ≥ `UPDOWN_HOURLY_OPPOSITE_MIN_MINUTES` (default 10m) to resolve
3. GBM decision.outcome ≠ locked_outcome (anti same-direction chase)
4. Standard GBM edge gate (≥ 5% post-fee) tetap apply
5. Slot cumulative cap (max 3 per slot) tetap apply

`_profit_locked_markets` sekarang `dict[condition_id → locked_outcome]` untuk track sisi yang udah TP.

---

## Risk Manager

- 2+ consecutive losses → cap **$10**
- 3+ consecutive wins → cap **$30**
- Default → **$20**
- `MAX_SAME_DIRECTION=2`, `MAX_POSITIONS_PER_SLOT=2`, `MAX_OPEN_POSITIONS=5`

---

## Circuit Breaker (`data/circuit_breaker.json`)

**`CB_ENABLED=False` di .env.local untuk paper trade phase.**
Setelah validasi 30+ trades, enable lagi sebelum live.

- Saklar 1: daily loss > 20% → pause sampai besok (auto-reset)
- Saklar 2: 5x consecutive loss → pause (manual reset)
- Saklar 3: drawdown > 40% → emergency stop (manual reset)

**Manual reset** — edit `data/circuit_breaker.json`:
```json
{
  "starting_capital": 120.0,
  "current_capital": <nilai sekarang>,
  "daily_loss": 0.0,
  "consecutive_losses": 0,
  "saklar_2_triggered": false,
  "saklar_3_triggered": false
}
```
`starting_capital` SELALU = 120 (SALDO_AWAL), bukan current_capital. CB hitung drawdown dari sini.

---

## Next Steps

| Priority | Task |
|---|---|
| 🔴 | Pantau 30+ trade hourly GBM, validasi winrate ≥ 60% & edge realisasi ≈ edge predicted |
| 🟢 | ~~Feed GBM `prob_up` ke Kelly winrate~~ — sudah aktif (`buy_winrate = clamp(prob_up, 0.50, 0.80)`) |
| 🟢 | ~~Tune `UPDOWN_HOURLY_GBM_MIN_EDGE`~~ — vol-adj threshold sudah aktif (DOGE→10%, BTC→4.4%) |
| 🟢 | ~~Exit Telegram alerts~~ — done, setiap exit (TP/SL) kirim alert ke bot |
| 🟡 | Cek log VOL per cycle — pastikan semua 6 symbol fetch OK (jangan fallback ke DEFAULT 40%) |
| 🟡 | Fix CB: cari kenapa `starting_capital` kadang berubah ke `current_capital` |
| 🟡 | Setup cron recalibrate di VPS |
| 🟢 | Go live (CB_ENABLED=True) setelah paper trade terbukti edge |

---

## Tests

```bash
pytest tests/ -q --tb=short --ignore=tests/test_async_binance.py
```
519 tests pass. Test files: `tests/test_*.py` (1:1 dengan modul di `src/`).
Pre-existing failures: `test_async_binance.py` (14 tests, unrelated to current work).
