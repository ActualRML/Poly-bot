# Polymarket Trading Bot

@STRATEGY_MISTAKES.md

## Agent Debate Protocol

For new features or complex logic, run this internal process **before writing any code**. Skip for simple tasks ("fix typo", "change color").

1. **Identity Split** — debate between:
   - *Architect*: scalability, design patterns, clean code
   - *Pragmatist*: simplicity, speed, no over-engineering
   - *Security/QA*: edge cases, vulnerabilities, error handling

2. **Debate Phase** — 1–2 rounds of disagreement/alignment

3. **Consensus** — agreed-upon approach

4. **Execution** — write code based on consensus

---

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
src/main.py               → entry point + main loop
src/scout/                → market discovery + signal generation
  scanner.py              → scan updown hourly markets
  regime.py               → cross-asset regime filter
  gbm.py                  → GBM probability model (directional fair-value)
  mispricing.py           → mispricing detector
  oracle_arb.py           → GBM math (gbm_prob_above)
  technical.py            → RSI, z-score, EMA, trend
src/risk/                 → sizing, circuit breaker, filters
  manager.py              → dynamic stop loss + position sizing
  kelly.py                → Kelly criterion sizer
  circuit.py              → circuit breaker (daily loss / consecutive loss / drawdown)
  blacklist.py            → per-symbol 4h pause after 3 consecutive losses
  slots.py                → slot cap + cumulative entry tracking
  pricing.py              → ke_decimal, hitung_midpoint, validasi_harga
  probability.py          → CryptoProbabilityCalculator (daily strategy)
  stagnation.py           → price stagnation tracker
src/execute/              → entry, exit, reentry, position mgmt
  exit.py                 → ExitEvaluator, PortfolioExitManager (TP + SL bands)
  reentry.py              → same-direction reentry logic
  reentry_mgr.py          → reentry candidate scanner + lifecycle
  scalping.py             → scalping exit signals
  position.py             → PositionManager (open/close/resolve)
  strategy.py             → dynamic threshold + force exit helpers
  candle.py               → candle strategy scanner + analyzer
  updown.py               → up/down daily strategy helpers
src/api/                  → CLOB, Gamma, Binance clients
src/utils/                → config, logger, parsing, telegram_alert
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

**Direction logic — `src/scout/gbm.py`**:
- Strike = Binance 1h candle open di start_date (cached per market)
- `P(Up) = gbm_prob_above(current, strike, vol_annual, T_remaining)` (closed-form GBM)
- `edge_up   = P(Up)     - market_price_up   - fee`
- `edge_down = (1-P(Up)) - market_price_down - fee`
- Pick side dengan edge ≥ `adj_min_edge`, else SKIP
  `adj_min_edge = max(UPDOWN_HOURLY_GBM_MIN_EDGE, vol_annual x UPDOWN_GBM_VOL_EDGE_FACTOR)`
  e.g. DOGE (100%) → 10%, BNB (56%) → 5.6%, BTC (44%) → 4.4%
- Toggle `UPDOWN_HOURLY_USE_GBM=false` → fallback ke contrarian lama

**Filter stack** (tiap entry harus lolos semua):
1. Slot cap: `MAX_POSITIONS_PER_SLOT=2` open + `_HOURLY_MAX_ENTRIES_PER_SLOT=3` cumulative
2. Per-symbol blacklist: 3 loss berturut-turut → pause symbol 4 jam (wired di evaluate_exits + resolve_checker)
3. Candle open delay: skip 5m awal candle (`UPDOWN_HOURLY_CANDLE_OPEN_MIN`)
4. Volume ratio >= `UPDOWN_HOURLY_MIN_VOL_RATIO` (default 0.5, vs baseline 30m)
5. Outcome price stagnation: skip kalau Polymarket price <0.5% range dalam 5m
6. Min/max entry: `0.20 <= buy_price <= 0.45`
7. Scalping signal gate (BTC): skip `WAIT_NOISE` / `WAIT_TREND` / km=0
8. Market regime filter (`src/scout/regime.py`):
   - Cross-asset: >=70% asset searah → +2
   - HTF 1h+4h alignment → +1
   - Session bias (US_OPEN) → +1
   - Score >= 4 → SKIP (only when `USE_GBM=false`; GBM mode bypass)
9. Liquidity check pre-entry (CLOB orderbook)
10. **GBM mode bypasses momentum-window gating** (edge gate sudah handle)

**`gap_pct` recorded di DB**: GBM mode → realized edge (e.g. 0.08 = 8%); contrarian mode → BTC 15m momentum (legacy).

**Sizing**:
- `buy_winrate = clamp(GBM prob_up, 0.50, 0.80)` — sisi yang dibeli; fallback 0.55 kalau GBM disabled
- Kelly bet x `kelly_multiplier` (0.5/0.75/1.0 dari ATR vs ATR_avg; x1.2 mom aligned, x0.75 mom opposed)
- Asia session: cap kelly_multiplier ke 0.7

---

## Exit Strategy untuk Hourly (`src/execute/exit.py`)

**Asimetris: profit lock cepat, SL hanya di akhir.**

### Profit Lock
| Tier | PnL trigger | Time gate |
|---|---|---|
| T1 | >= 200% | > 5m left |
| T2 | >= 150% | > 15m left |

Selain itu → HOLD ke resolve untuk full payout.

**Exit alerts**: setiap exit kirim `alert_exit` ke Telegram + `log.info [EXIT]`.
Format: `[EXIT] {SIGNAL} — {question} | {outcome} @ entry→exit | PnL $X`

### Late-Stage SL
| Band | Time range | Threshold |
|---|---|---|
| OUTER | 10–20m left | PnL <= -30% |
| MIDDLE | 5–10m left | PnL <= -50% |
| INNER | 0–5m left | PnL <= -70% |

> 20m left → NO SL (kasih ruang recovery).

---

## Re-entry After Take-Profit

### Same-direction (`src/execute/reentry.py`)
1. Drop >= 30% dari exit_price
2. Fair value > current_price + fee + 5% edge
3. Orderbook: spread <= 5%, liquidity cukup
4. Time gate: >= 15m to resolve
5. Slot cumulative cap belum penuh
→ Re-entry @ **half size**.

### Opposite-direction (`src/scout/gbm.py:passes_opposite_reentry_gate`)
1. `UPDOWN_HOURLY_OPPOSITE_REENTRY=true`
2. Time floor: >= `UPDOWN_HOURLY_OPPOSITE_MIN_MINUTES` (default 10m)
3. GBM decision.outcome != locked_outcome
4. Standard GBM edge gate tetap apply
5. Slot cumulative cap tetap apply

---

## Risk Manager

- 2+ consecutive losses → cap **$10**
- 3+ consecutive wins → cap **$30**
- Default → **$20**
- `MAX_SAME_DIRECTION=2`, `MAX_POSITIONS_PER_SLOT=2`, `MAX_OPEN_POSITIONS=5`

---

## Circuit Breaker (`data/circuit_breaker.json`)

**`CB_ENABLED=False` untuk paper trade phase.**

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
`starting_capital` SELALU = 120 (SALDO_AWAL). CB hitung drawdown dari sini.

---

## Tests

```bash
pytest tests/ -q --tb=short --ignore=tests/test_async_binance.py
```
Tests dikosongkan saat refactor. Tulis ulang setelah strategy stabil.
Pre-existing failures: `test_async_binance.py` (14 tests, unrelated).
