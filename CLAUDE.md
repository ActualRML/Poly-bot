# CLAUDE.md — polymarket-bot

> **Research verdicts + the evidence behind them live in [FINDINGS.md](FINDINGS.md).**
> This file = operational orientation: what exists, how to run it, current status.
> A fresh Claude should be productive from this file alone.

## Project Overview
Async Python **paper-trading** bot for **Polymarket hourly crypto Up/Down markets**
(BTC/ETH/SOL/XRP/DOGE/BNB). Streams Polymarket CLOB + Binance spot, classifies market
state, runs pluggable strategies, simulates a bankroll. **`DRY_RUN=True` by default.**
⚠️ **Live trade execution is NOT built** — flipping `DRY_RUN=False` only logs a warning;
there is no order signing/placement/fill-reconciliation. The whole stack is a paper sim.

## Current Architecture
```
src/main.py            async orchestrator run(): discovery → WS clients → dispatch → loops
src/api/
  polymarket.py        REST (read-only): discover_updown_markets, get_market_resolution
  polymarket_ws.py     CLOB market-channel WS (book / price_change / last_trade_price)
  binance_ws.py        miniTicker spot WS (6 symbols);  ws_base.py = shared reconnect base
src/data/
  snapshot.py          MarketSnapshot — normalized view; price is YES-perspective (NO→1−p).
                       Carries book bid/ask size+depth, and resolve_time (for time-aware gates).
  parsers.py           parse_polymarket / parse_binance → MarketSnapshot (book price = best_bid)
  db.py, schema.py     aiosqlite; tables: decisions, snapshots, positions, balance (ALTER-migrated)
  writer.py            batched writer; throttles price_change/ticker→~10s, keeps book/last_trade FULL
src/classify/
  price_zone.py        YES price → extreme_low/low/uncertain/high/extreme_high (calibrated, config-driven)
  volatility.py        spot rolling pstdev → low/mid/high_vol (DISCARDS direction)
src/strategy/
  base.py              Strategy ABC: async evaluate(snapshot)->Decision; ISOLATION — strategies see
                       ONLY MarketSnapshot (no api/db imports; add a field + populate in orchestrator)
  params.py            StrategyParams(entry_floor, bet_fraction, entry_ceiling) — the ONLY per-strategy knobs
  contrarian.py        THE live strategy. extreme_* zone + low_vol → fade/bet reversion (buy the cheap
                       side ≤ entry_ceiling=0.30). + reversion-RUNWAY gate: SKIP entry if ≤10 min to
                       resolve (MIN_RUNWAY_SECONDS=600, measured vs snapshot.ts → replay-safe).
                       (breakout DEAD/removed; hv re-tested 2026-06-20 DEAD/removed; mv re-tested 2026-06-20
                       → regime-CONDITIONAL like LV, now a LIVE canary (BTC/ETH) — see contrarian_mv.py.)
  momentum.py          CANARY (2026-06-18): follow-the-EXTREME-favorite = mirror of contrarian (same
                       trigger, buys the FAVORITE; entry_ceiling=0.85; NO runway gate — follow wins late).
                       PRIOR DEAD; forward-paper MEASUREMENT only, runs ALONGSIDE contrarian (per-strategy
                       is_held). KILLED 2026-06-19 (kill-line hit: n=151, avg_roi −0.10, −$759); file kept,
                       NOT in ACTIVE_STRATEGIES. FINDINGS *2026-06-18 cont.*
  contrarian_mv.py     CANARY (2026-06-20): mid_vol twin of contrarian — SAME fade logic/params, vol gate =
                       mid_vol, scoped BTC/ETH (SYMBOLS const; thin coins dead by fill). Regime-CONDITIONAL
                       like LV (re-test: MV-revert ≥ LV-revert in the blind backtest lens). SCORE the
                       REVERT-SUBSET only (NOT total avg_roi — efficient bleed is by-design); KILL only if
                       revert-subset clearly negative. Held-to-resolution. FROZEN. FINDINGS *2026-06-20*.
src/execute/
  decision.py          Decision / Action(BUY|SELL|SKIP)
  executor.py          DryRunExecutor (logs decisions; live path intentionally NOT implemented)
  fill.py              REALISTIC taker fill (simulate_taker_fill: lift ask + walk depth, limit-priced by
                       entry_ceiling → recorded entry = effective fill, NOT mid). SELL mirror
                       (simulate_taker_sell: hit bid + walk down; empty/one-sided bid → no_exit = hold)
  portfolio.py         sim bankroll. GLOBAL non-overridable limits: MIN_BET_USDC=1,
                       MIN_TIME_TO_RESOLVE_SEC=120, MAX_BOOK_AGE_SEC=30 (skip fill vs >30s-stale book).
                       SLIPPAGE_BUFFER=0.0 (DEPRECATED — cost modeled at fill). is_held dedup PER-(market,
                       strategy) + get_open(strategy=) so contrarian + momentum coexist on one market;
                       open/resolve/void/close_position (early exit: full-fill-or-hold → status='closed')
  resolver.py          hold-to-resolution settle (exit 1.0/0.0); force-void after 6h stuck
  exits.py             per-tick EXIT overlays on HELD positions (book events), wired in main. Strategies
                       only ENTER; exits live here (EXIT_STRATEGY='contrarian' → act on contrarian
                       positions ONLY, never the momentum canary's favorites):
                       • maybe_stop_loss — time-gated SL (value ≤0.10, final ≤2 min, BTC/ETH;
                         sl_canary_enabled=ON). VALIDATED. FINDINGS *Exits*.
                       • maybe_slow_rise_exit — sell at 0.40 if open→0.40 climb was SLOW (>10 min);
                         slowrise_enabled=ON (canary since 2026-06-18; closes tagged 'slow_rise_exit').
                         Frozen params — score forward (net salvage + winners-killed), don't tune. FINDINGS *Exits*.
src/monitor/{logger,health}.py   structured logging + heartbeat
src/notify/{telegram,tele_server}.py   optional Telegram /status server (standalone, NOT wired into bot)
src/config.py          pydantic Settings; dry_run=True; ACTIVE_STRATEGIES=contrarian (.env.local; momentum KILLED 06-19);
                       zone/vol thresholds; SL + slow-rise knobs; db_path=data/bot.db
src/backtest/          snapshot-replay harness (CLI `python -m src.backtest`): recovery.py (offline
                       resolve_ts+outcome, touch-only), engine.py (SimPortfolio + replay, REALISTIC fill
                       = simulate_taker_fill same as live; token-polarity reconstruction; book cache;
                       fixed_bet_usdc sizing-neutral mode), report.py, diagnostics.py, conditional_miner.py
scripts/
  make_bt_db.py        indexed read-only backtest copy of bot.db → data/bot_bt.db (~30s; keeps ALL poly
                       events — price_change is load-bearing for trigger fidelity, do NOT slim it)
  check_state.py       read-only dump of the sim ledger (balance, open/resolved, per-strategy scoreboard)
research/              READ-ONLY analysis scripts (run manually) → research/diagnostics/. Canonical:
                       verify_labels.py (Polymarket API → label_truth.csv ground truth), calibrate_zones.py,
                       the exit/SL probes; the rest are per-finding runners cited in FINDINGS.
```
**Data flow:** WS event → parser → MarketSnapshot → classify (`vol_regime`, `price_zone`) →
each `strategy.evaluate()` → if BUY & not `is_held` → executor + `portfolio.open_position`
(realistic `simulate_taker_fill`: stale-book gate + entry_ceiling limit-price) → `resolver`
settles at resolution → SQLite. Per-tick exits (`exits.py`) can close held positions early.

## Working Rules
- **Run tests:** `.venv/Scripts/python.exe -m pytest -q` (global python lacks aiosqlite; `asyncio_mode=auto`).
- **Backtest = NEGATIVE SCREEN, not positive-confirm.** Build `data/bot_bt.db` via
  `python scripts/make_bt_db.py`, then `python -m src.backtest`. Engine fills REALISTICALLY (same as
  live). It KILLS mirages (caught hv +$879→−0.25) but selects different markets than live, so it
  **understates** and CANNOT certify a survivor like contrarian — the **live forward run is the
  authority** there. Verdicts are still one ~24h window. FINDINGS *Methodology canon*.
- **Research = DEEP + multi-condition BY DEFAULT (don't wait to be asked to add a cut).** Testing a
  signal/hypothesis means, in ONE pass: segment across REGIMES (revert/efficient, boundary per FINDINGS),
  COINS, and a chronological TRAIN/TEST split; labels from the LIVE ledger (authority). Because more
  conditions = more multiple-testing, every sweep is a NEGATIVE SCREEN that MUST carry the guards or it
  doesn't count: per-regime PLACEBO + BEST-OF-N noise floor, ORTHOGONALIZE candidates vs price/vol (most
  "edges" are price proxies), AUC over tuned thresholds, and report EVERY condition incl. nulls. "Real" =
  beats best-of-N AND holds train→test AND survives orthogonalization. Templates: research/probe_*.py
  (probe_candle_filter, probe_mv_retest, probe_winloss_chars).
- **Offline labels = touch-only decisive rule** (`event_type IN book/last_trade_price`; NEVER
  `price_change`). Canon in `recovery.py` + every probe replica; ground truth
  `research/diagnostics/label_truth.csv`. Recovery EXCLUDES in-flight markets at the data edge.
- **Strategy isolation:** strategies must NOT import `src.api`/`src.data` — add a field to
  MarketSnapshot and populate it in the orchestrator instead.
- **Env:** Windows + PowerShell. DRY_RUN needs zero credentials; LIVE (unbuilt) would need
  PK_PRIVATE_KEY / CLOB_API_KEY / CLOB_SECRET / CLOB_PASS.

## Current Status (2026-06-25)
The **profit question is ANSWERED for this regime**: hourly crypto Up/Down is **efficient** — no taker OR
maker edge survives realistic cost, and the within-market signal search is EXHAUSTED. Evidence + numbers:
FINDINGS (through-line + *Session 2026-06-17/18*). Orientation summary below.
- **⭐ Edge is regime-CONDITIONAL, NOT switchable.** contrarian flips **+15.5pp (revert ≤06-15) → −5.6pp
  (efficient ≥06-16)**, same params. Regime is UNDETECTABLE ex-ante (proof in *Don't* + FINDINGS *2026-06-18
  cont.*); the real lever is a different market structure.
- **Survivor = contrarian (LV, low_vol fade)** — a real WR edge but **marginal-after-fills**; honest measure =
  the **era-2 realistic-fill subset** (the +$11k paper ledger is era-1 mid-fill inflated). FINDINGS *2026-06-17/18*.
- **⭐ contrarian_mv RE-TESTED (2026-06-20) — NOT dead; regime-CONDITIONAL like LV.** On the fill-INDEPENDENT
  edge, MV-revert (BTC/ETH) +4.9pp (z2.1) ≥ LV-revert in the same (blind) backtest lens → backtest FAILS to
  kill it. Was a LIVE canary (06-20, BTC/ETH) → **REMOVED 06-23** (revert window never came → revert n=0,
  unscoreable; see TODO #3). FINDINGS *2026-06-20*.
- **Bot:** DRY-RUN, `ACTIVE_STRATEGIES=contrarian` + runway gate (≤10min skip); **SL + slow-rise canaries
  ON** (forward paper). momentum KILLED 06-19. **contrarian_mv REMOVED 06-23** — dormant: started 06-20 in
  efficient, revert window never came → **revert n=0, unscoreable, no-ETA**; low-value (persist supersedes);
  can't regime-filter (trailing-WR whipsaws, n=1). `contrarian_mv.py` KEPT (backtestable, negative-screen
  only). **Only LV (contrarian) is live.**
- **Open threads / TODO:**
  1. **Score era-2 realistic contrarian PnL at n≥~100** (honest edge, not the inflated paper +$11k) — go-live gate.
  2. **⭐ persist FORWARD TEST (FROZEN 2026-06-21) — strongest candidate ever.** `persist` (mean fade-side
     depth imbalance over 12m pre-entry) = the depth candidate done RIGHT (temporal); SUPERSEDES static
     depth_ratio (logistic: static depth → insignificant once persist is in). Passed the full stack
     in-sample (efficient AUC 0.74, within-coin, logistic p=0.008, PnL +0.5/+0.7) BUT CIs THIN (only BTC/ETH
     Tier2 avg_roi excludes 0). Pre-registered tiers `persist>−0.169` / `>−0.100`, forward = efficient
     trades ≥06-22, MEASURE-ONLY (no gating → does NOT slow the run / depth-n, still the binding bottleneck).
     Loop: rebuild bot_bt.db → `research/filter_test.py` (the pre-reg SCORER; `probe_persist_forward.py` is
     now the DIAGNOSTIC) as efficient n accrues; confirm = forward avg_roi CI > 0 at n~30-50. FROZEN — no
     shift, no tune. `depth_ratio` ex-ante-knowable, NOT a regime switch. **Forward 06-24: n=34
     (Tier1=15/Tier2=7) — separation HOLDS directional+monotonic (below-T1 −0.354 → Tier2 +0.196) but
     UNDERPOWERED (CIs span 0). ⚠️ live-positions equity (+35% Tier2) does NOT survive RAW-reconstruction
     (engine, 417 trades: ALL lose, persist barely separates) → +35% was SELECTION-favorable, confidence
     DOWN (not dead — direction holds). Confirm still = CI>0 @ Tier1 30-50 (~1wk).** FINDINGS *2026-06-21
     cont. + 06-23 + 06-24*.
     **⭐ FORMALIZED 2026-06-25 = the pre-registered FILTER TEST (LOCKED).** Overlay on the UNCHANGED
     contrarian (filter may only SKIP, scored OFFLINE — live bot still takes all, not gated). Two instruments:
     **arm A persist** = `research/filter_test.py` (per-arm scoreboard Baseline/Tier1/Tier2; realistic-fill
     LEDGER is the gate-lens, `--engine` adds the raw-reconstruction = gap-#1 cross-check that LAGS the data
     edge → engine forward fills in as markets age); **arm B holistik** = discretionary overlay
     `scripts/filter_b.py` (`list`/`call`/`score`, FORWARD-ONLY, logs `data/filter_b_calls.jsonl`, never
     touches the entry engine). LOCKED gate (Tier1 PRIMARY, Tier2 secondary): `margin ≥ break-even+8pp` AND
     `N≥100`/arm, evaluate-ONCE (no peek-and-extend; do NOT tune tiers/buffer/window). **06-25: arm A forward
     Baseline N=44 / Tier1 N=19 (margin +0.3pp) / Tier2 N=8 → NO CONCLUSION, accruing (Tier1 needs +81); arm
     B N=0.** memory `project_filter_test_preregistered_2026_06_25`.
  3. **contrarian_mv — REMOVED 06-23** (was a canary; never got a revert window → revert n=0, unscoreable,
     no-ETA; low-value, persist supersedes; can't regime-filter [trailing-WR whipsaws]). File KEPT,
     backtestable (negative-screen only). Re-enable live ONLY in a future revert window IF persist fails.
  4. **Score slow-rise canary** at ≥~100 `slow_rise_exit` closes → keep / set `SLOWRISE_ENABLED=false`. FROZEN.
  5. **Go-live (real money)** = a real BUILD (signing/placement/reconciliation/rails — none exist) + tiny modal +
     pre-registered kill-line. ONLY after (1) clears breakeven AND a 2nd regime.

## Don't (hard-won — see FINDINGS for why)
- **Don't believe one window.** Momentum's 72.7% bull-window WR was drift, not edge. A 6-trade loss
  streak is noise (06-15 looked like collapse, fully recovered).
- **Don't knob-hunt / mine signals.** Every within-regime "edge" found by searching N signals flipped
  NEGATIVE out-of-sample (hv 7-signal search, exit families). Searching manufactures TRAIN winners.
- **Don't tune a strategy mid-measurement** (voids the forward test). Don't re-run concluded dead loops.
- **Don't build a regime switch / strategy-router (now).** The regime tell is strongly RESISTED — NOT
  *proven* impossible (overclaim corrected 2026-06-22), but placebo-negative (best +20pp < noise +27pp) +
  efficient-market prior; the separators tried were all OUTCOME-based, so a MICROSTRUCTURE-regime signal is
  the one UNTESTED door (low-EV). The OTHER door = a MACRO-regime tell (VIX/BTC-RV/funding/…): PARKED —
  n=1 transition → ~0 validatable DoF; retro probe 2026-06-23 (`research/probe_macro_regime.py`) showed macro
  DID differ (VIX/vol higher in revert, plausible) but n=1-UNATTRIBUTABLE. Macro is RETROACTIVELY FETCHABLE
  (NO forward logger — re-run the probe when transitions accrue; src/macro logger was built then DELETED as
  redundant). And **persist routes around the need** (regime-agnostic per-trade filter) → a detector is
  largely UNNECESSARY. A trailing-WR switch throws away winners (loss streaks = normal noise at ~33% WR).
  **CORRECTED 06-24: macro is low-EV REGARDLESS of persist (its outcome changes macro's RELEVANCE, not its
  EV). Honest call: persist fails → STOP (accept the efficient market), NOT chase the low-EV macro door —
  that's a maybe-someday curiosity IF ≥~10 transitions ever accrue, NOT a fallback plan.**
- **Don't throw ML at this market.** Efficient (small predictable residual) + tiny sample (~hundreds of
  independent markets, n=1 regime) → ML = the overfit trap AMPLIFIED (bigger search → more train-winners that
  die forward). persist (the best focused signal) caps the findable edge. Worth only with MUCH more data; the
  lever = a different market STRUCTURE, not a fancier model. FINDINGS *2026-06-24*.
- **Don't re-test the 2026-06-23 kills (read-only autopsies in FINDINGS):** wide-spread × late-entry EV
  (@MID calibration ≈0 = price FAIR; spread = monotonic COST not signal; `research/probe_late_entry_ev.py`);
  inventory-unwind near expiry (binaries SELF-SETTLE → holders DOUBLE-DOWN not unwind, opposite of the
  hypothesis; `research/probe_inventory_unwind.py`).
- **Don't delete tests because a finding is in FINDINGS** — tests guard live code, docs record why.

## Things NOT True Anymore (corrections)
- Pre-`3a97b1f` design (scout/risk/filters/Kelly/GBM/`bot_database.db`) is fully obsolete — ignore any
  such memory/comment. Backtest harness is `src/backtest/` (`python -m src.backtest`).
- `SLIPPAGE_BUFFER=0.0` (DEPRECATED): execution cost is modeled at entry by `fill.py` (ask + depth
  walk), so `positions.entry_price` is the effective taker fill, not the YES mid. Any "0.03 buffer" doc is stale.
