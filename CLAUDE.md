# Project Context

Polymarket prediction-market bot. Asynchronous Python loop that scans Polymarket "Up/Down hourly" binary markets for BTC/ETH/SOL/XRP/DOGE/BNB and bets WITH 15m momentum.

- **Only active strategy**: `updown_hourly_momentum` (single entrypoint loop).
- **Default mode**: `DRY_RUN=True`, `CB_ENABLED=False`, modal $120 (`SALDO_AWAL`).
- Live trading possible (`pasang_order` wired to py-clob-client) but no fill-confirmation polling.

@STRATEGY_MISTAKES.md

## Architecture

```
src/main.py                    → entrypoint, spawns single loop
src/execute/loop.py            → cycle: scan → regime → cycle_gate → per-market analyze → exit eval
src/scout/scanner.py           → Gamma /events discovery (slug-prefix match)
src/scout/updown_hourly.py     → per-market driver (build context → evaluate_entry → place order)
src/scout/scout.py             → 5-stage filter pipeline w/ short-circuit
src/scout/filters/*.py         → DISCOVERY(0) PRECHECK(6) SIGNAL(10) RISK(4) EXEC(4)
src/execute/exit.py            → vol-scaled TP/SL bands per strategy_mode
src/risk/{kelly,manager,slots,blacklist,stagnation,pricing}.py
src/api/{clob,gamma,binance}_client.py
src/models/database.py         → SQLite (positions, trades, predictions)
```

Data flow: `scan_updown_hourly_markets` → `ScoutCycleGate.evaluate` → `analyze_updown_hourly_market` → `ScoutContext.build` → `evaluate_entry` (24 filters) → `SizingFilter` (Kelly × multipliers) → `clob.pasang_order` → `manager.open_position` → SQLite.

## Key Files

| Purpose | Path:line |
|---|---|
| Entrypoint | `src/main.py:11` |
| Main loop body | `src/execute/loop.py:40` `run_hourly_updown_mode` |
| Filter pipeline | `src/scout/scout.py:40` `evaluate_entry` |
| Direction decision (momentum sign) | `src/scout/filters/signal.py:87` |
| Sizing (Kelly × km × session cap) | `src/scout/filters/exec.py:22` `SizingFilter` |
| Exit bands (T1–T4 SL, T1/T2 TP, anytime 150%) | `src/execute/exit.py:259` |
| Position write/read | `src/execute/position.py:72` `open_position` |
| DB schema + migrations | `src/models/database.py:29` |
| Cycle-wide gate (CB / flash-crash) | `src/scout/cycle.py:18` |
| Heuristic winrate (6-bin score) | `src/scout/probability.py:39` |

## Active vs Disabled Features

| Feature | Flag | Default | Code path | Status |
|---|---|---|---|---|
| Hourly momentum entry | — | always on | `loop.py:702` | ACTIVE |
| Flash-crash hard skip | `FLASH_CRASH_HARD_SKIP` | True | `cycle.py:65` | ACTIVE |
| Anytime TP at +150% | `UPDOWN_HOURLY_LOCK_ANYTIME_PCT` | 150.0 | `exit.py:331` | ACTIVE |
| Candle 1h-resolve strategy | `CANDLE_ENABLED` | False | `execute/candle.py` | DISABLED (file loaded) |
| Hourly-flip (loss-reversal) | `HOURLY_FLIP_ENABLED` | False | `loop.py:251,494` | DISABLED |
| Max-momentum cap filter | `FILTER_MOMENTUM_CAP_ENABLED` | False | `filters/signal.py:35` | DISABLED |
| Macro-regime trend gate | `UPDOWN_HOURLY_MACRO_TREND_GATE` | False | `cycle.py:77` | DISABLED |
| Circuit breaker | `CB_ENABLED` | False | `_archive/circuit.py` via `loop.py:15` | DISABLED |
| Candle reverse re-entry | (depends on `CANDLE_ENABLED`) | — | `execute/reentry.py` | DORMANT |

## State Persistence Map

| State | Location | Survives restart? |
|---|---|---|
| positions, trades, predictions | SQLite `data/bot_database.db` | YES |
| circuit breaker | `data/circuit_breaker.json` | YES (when enabled) |
| TARIK flag | `data/tarik.flag` | manual file trigger |
| **slot history counter** | `_hourly_slot_history` dict in `risk/slots.py:5` | **NO** |
| **symbol blacklist** | `_symbol_blacklist_until` dict in `risk/blacklist.py:7` | **NO** |
| **market price stagnation** | `_market_price_history` dict in `risk/stagnation.py:3` | **NO** |
| `_profit_locked_markets`, `_candle_sl_markets`, `_hourly_flip_queue` | locals in `run_hourly_updown_mode` | **NO** |
| Binance price/vol/klines cache | module globals in `api/binance_client.py:24-49` | NO |

## Risk Parameters (live values, from `src/utils/config.py`)

| Param | Default | Notes |
|---|---|---|
| `BASE_SIZE_PCT` (constant) | 0.08 | hardcoded at `risk/manager.py:15`; new fixed-fractional sizer |
| `MIN_BET_USDC` | 5.0 | KellySizer min — Kelly path deprecated |
| `MIN_POSITION_USDC` / `_MAX` (constants) | 3 / 75 | hard floor/ceiling at `risk/manager.py:9-10` |
| `MAX_OPEN_POSITIONS` | 10 | |
| `MAX_CAPITAL_PER_MARKET` | 75.0 | single SoT now (constants removed from `position.py`) |
| `MAX_SAME_DIRECTION` | 2 | .env.example says 3 (env wins) |
| `MAX_POSITIONS_PER_SLOT` | 5 | open posn cap per resolve slot |
| `UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT` | 6 | cumulative cap per slot |
| `UPDOWN_HOURLY_MIN_T_MINUTES` | 20 | entry time-floor |
| `UPDOWN_HOURLY_MIN_ENTRY_PRICE` / `_MAX_ENTRY_PRICE` | 0.25 / 0.65 | price band |
| `UPDOWN_HOURLY_MOMENTUM_MIN` | 0.0015 | thr = max(MIN, vol_15m × VOL_FACTOR) |
| `UPDOWN_HOURLY_MOMENTUM_VOL_FACTOR` | 0.75 | |
| `EV_GATE_ENABLED` | True | reject `buy_price > winrate − margin` |
| `EV_GATE_MIN_MARGIN` | 0.02 | with flat winrate=0.50 → threshold 0.48 |
| `HOURLY_LOCK_T1_PCT` / `_T2_PCT` | 80 / 50 | .env.example T2=70 (DIVERGES) |
| `UPDOWN_HOURLY_LOCK_ANYTIME_PCT` | 150.0 | late-window safety TP |
| `HOURLY_SL_MIN_AGE_MINUTES` | 10 | grace before T3/T4 SL fires |
| `HOURLY_FLIP_TRIGGER_PCT` | -40 | .env.example -50 (DIVERGES) |
| `POLLING_INTERVAL_DETIK` | 5 | .env.example 30 (DIVERGES) |
| `SALDO_AWAL` | 120 | |
| `DRY_RUN` / `CB_ENABLED` | True / False | |

Per-coin vol scaling for SL/TP is **hardcoded** at `exit.py:13-16` (BTC 0.44 .. DOGE 1.00) — not configurable.

## Per-Symbol Sizing (interim, 2026-05-23)

`size = clamp(capital × BASE_SIZE_PCT × multiplier, MIN_POSITION_USDC, MAX_POSITION_USDC)`

| Symbol | Multiplier | Effective %cap (raw) | Size at $120 capital |
|---|---|---|---|
| BTC  | 1.0 | 8.0% | $9.60 |
| ETH  | 0.6 | 4.8% | $5.76 |
| SOL  | 0.6 | 4.8% | $5.76 |
| BNB  | 0.5 | 4.0% | $4.80 |
| DOGE | 0.4 | 3.2% | $3.84 |
| XRP  | 0.3 | 2.4% | $3.00 (clamped to MIN=$3) |
| _unknown_ | 0.5 | 4.0% | $4.80 (DEFAULT_SYMBOL_MULT) |

With `BASE_SIZE_PCT=0.08` and `MIN_POSITION_USDC=$3`, per-symbol differentiation is **active at SALDO_AWAL=$120**: BTC bets ~3.2× XRP. Only XRP clamps to the floor.

## Strategy State (Interim, post-2026-05-23 refactor)

Flat probability mode. Bot trades based on hard filters + EV gate (implicit buy_price ≤ 0.48). No score-based confidence weighting. KellySizer still runs but its output is capped by the deterministic fixed-fractional sizer. Re-evaluate after ≥500 trades collected in this mode.

## Known Issues (verified from code, not folklore)

- **In-memory amnesia**: slot counter, symbol blacklist, stagnation tracker, profit-locked-markets, flip-queue all live in dict() globals or loop locals — wiped on every restart. Reentry guards and "3-loss blacklist" do NOT survive process restart.
- **No fill confirmation**: `clob_client.py:111` `pasang_order` treats `resp.get("orderID")` truthy as success. No FILLED-status polling; partial fills not handled.
- **Config divergence (remaining)**: `MAX_SAME_DIRECTION`, `HOURLY_LOCK_T2_PCT`, `HOURLY_FLIP_TRIGGER_PCT`, `POLLING_INTERVAL_DETIK` still differ between `.env.example` and `config.py` defaults — env wins at runtime.
- **CANDLE_ENABLED=false but file still imports**: `execute/candle.py` imports many modules unconditionally; turning the flag on would surface bit-rot.
- **Comment vs code mismatch**: `STRATEGY_MISTAKES.md` and several inline comments describe a "contrarian" strategy; current `DirectionalDecisionFilter` (`filters/signal.py:92-97`) is **momentum-following**.
- **Pydantic dep orphaned (2026-05-23)**: `requirements.txt` keeps `pydantic>=2.0.0` but the only user (`models/scout.py`) was deleted. Left in place — out of scope to remove.
- **4 async tests in `test_scout_filters.py` fail** without pytest-asyncio plugin (pre-existing, unrelated to refactor).

## Dev Workflow

```bash
# Run (DRY_RUN default = paper trade)
python -m src.main

# Tests (94 collected)
pytest tests/ -q --tb=short

# Backtest filter-rejection analysis (NOT predicted PnL)
python -m script.backtest
python -m script.backtest_filter

# Live monitor (stdout)
python -m script.monitor

# Telegram interactive bot
python -m script.monitor_bot

# Force-close all open positions
echo "" > data/tarik.flag       # or comma-separated condition IDs
```

Env vars required for LIVE only (`config.py:133-144`): `PK_PRIVATE_KEY`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS`. DRY_RUN runs with zero credentials.

Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Workflow Rule: Strategy Changes Backtest First

Logic changes (filters, sizing, exit thresholds): backtest → 7d DRY_RUN → LIVE. Config-only changes that tighten risk may skip backtest. Loosening any risk param → backtest required. Anti-pattern: shipping new logic straight to DRY_RUN to "see what happens."

Token-saving (CRITICAL): no filler, no re-reads, diff-only edits, pipe terminals through `| Select-Object -Last 50`.

## Things NOT True Anymore (purge from memory)

- ❌ "GBM probability model is primary" → ✅ GBM toggle removed from `.env.example`; momentum-sign is the only directional signal (`filters/signal.py:92`). `UPDOWN_HOURLY_USE_GBM` not in `config.py`.
- ❌ "Strategy is contrarian (bet against 15m momentum)" → ✅ Code bets WITH the 15m sign (`filters/signal.py:92-97`). `strategy_mode = "updown_hourly_momentum"`.
- ❌ "`gap_pct` = GBM realized edge" → ✅ Stored as `abs(btc_regime or 0.0)` (`updown_hourly.py:96`) — BTC's own 15m momentum magnitude, no longer edge-related.
- ❌ "`buy_winrate = 0.33` hardcoded placeholder" → ✅ `scout/probability.py:39` `calculate_winrate` returns score-based 0.20–0.80 from 6 signals.
- ❌ "Daily crypto + Up/Down daily strategies active" → ✅ Only `run_hourly_updown_mode` is wired in `main.py:33`. Daily-strategy classes still exist in `exit.py` but unused.
- ❌ "Candle scalper / reentry / opposite-flip / GBM all active" → ✅ All gated off (`CANDLE_ENABLED=false`, `HOURLY_FLIP_ENABLED=false`); `execute/reentry.py` only callable from `candle.py`.
- ❌ "`buy_winrate=0.33` documented as historical baseline" → ✅ Removed; winrate now per-trade from probability model.
- ❌ "`UPDOWN_HOURLY_USE_GBM=false` fallback to contrarian" → ✅ Toggle gone, only momentum path remains.
- ❌ "`buy_winrate` placeholder in context.py" → ✅ `ScoutContext.buy_winrate` is set live in `DirectionalDecisionFilter.evaluate` (`filters/signal.py:108`).
- ❌ "Re-entry after take-profit (same-direction half-size, opposite-direction GBM)" → ✅ Both paths removed from source; only `_profit_locked_markets` dict (loop local) is consulted by `ProfitLockedFilter`.
- ❌ "Risk manager caps: 2 losses → $10, 3 wins → $30, default $20" → ✅ Now percent-based via `RISK_BASE_SIZE_PCT=0.15`, `_MIN=0.08`, `_MAX=0.40` (`risk/manager.py:43`).
- ❌ "GBM-mode bypasses momentum-window gating" → ✅ No GBM mode exists.
- ❌ "MAX_POSITIONS_PER_SLOT=2, MAX_OPEN_POSITIONS=5" → ✅ Now 5 and 10 respectively.
- ❌ "SCORE_TO_PROB 6-bin lookup drives winrate" → ✅ flat 0.50, probability claim dropped (interim, see 2026-05-23 refactor; `scout/probability.py`).
- ❌ "Half-Kelly sizing × 0.7 multiplier is the sizer" → ✅ fixed-fractional 5% capital × per-symbol multiplier (`risk/manager.py`). KellySizer still instantiated but its bet is capped by the new sizer.
- ❌ "`MAX_CAPITAL_PER_MARKET` shadowed di `position.py:26`" → ✅ single source: `config.py` only. Module constants removed.
- ❌ "`HOURLY_MAX_ENTRIES_PER_SLOT = 8` hardcoded di `slots.py:6`" → ✅ constant removed; 3 callers updated to read `config.UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT` directly.
- ❌ "`loop.py:46,50,70,71` getattr fallbacks drift from config defaults (0.52, 30.0, 0.30, 0.10)" → ✅ aligned to (0.15, 75.0, 0.40, 0.20).
- ❌ "`src/_archive/circuit.py` imported live from arsip path" → ✅ moved to `src/risk/circuit.py`; `_archive/` folder removed.
- ❌ "`models/scout.py` `ScoutSignal` orphan" → ✅ deleted.
- ❌ "`google-genai` declared but zero usage" → ✅ removed from `requirements.txt`.
- ❌ "Bot enters at any price within `[MIN_ENTRY, MAX_ENTRY]` band" → ✅ additional `EvGateFilter` rejects `buy_price > buy_winrate − EV_GATE_MIN_MARGIN`. With flat 0.50 winrate, effective cap is 0.48.
- ❌ "BASE_SIZE_PCT=0.05 + MIN=$10 floor (all symbols clamp at $120 capital)" → ✅ 0.08 + MIN=$3 (per-symbol differentiation active at $120: BTC $9.60, XRP $3.00).
