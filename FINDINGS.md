# FINDINGS.md — polymarket-bot research record

What we've tested and concluded, with the key numbers and re-run commands. Consulted during
research — not needed for routine operation (that's `CLAUDE.md`). **Every verdict is regime-limited
(one ~24h–2day bull window) and sample-limited (throttled stream)** — read AGGREGATE direction, not
thin cells. PnL math is exact; verdicts are not regime-proof. All work READ-ONLY on `data/bot.db`
unless noted.

## Through-line — the profit question is ANSWERED for this regime
Hourly crypto Up/Down is **efficient: entry price ≈ win rate**. No accessible **taker** edge —
directional, momentum, cross-coin, order-flow imbalance, and return-conditioning all tested DEAD on
clean labels (orthogonalized, positive-controlled, cluster-corrected). What's left in clean data is
the window's own **+10pt bull drift** — regime, not edge. The structurally-different option,
**maker / earn-the-spread**, is also **DEAD offline** (adverse selection). So both edge classes are
closed; what remains is one survivor under a one-regime caveat (**contrarian**) and the engineering
outcome. **Goal = a correct, measurable bot, not a money machine** — the foundation is correct and
tested; it does not manufacture profit in an efficient market.

External corroboration (ecosystem scan, 2026-06-09): taker directional edge in hourly crypto is dead
across multiple projects; profitable crypto wallets are spread-capturers (makers) not copyable at
market price; the credible MM repo **poly-maker** (~964★) states MM is **not profitable now** (crowded);
cross-platform arb needs **>~$500k volume**. Don't reopen "is there an edge" from scratch.

## Session 2026-06-24 — persist forward (n=34) + the live-vs-RAW backtest DIVERGENCE (the "+35%" was selection-favorable)
Refreshed the forward test and ran the $/equity backtest TWO ways; the lenses DISAGREE → persist NOT confirmed, and the live-positions $ picture is optimistic/selection-confounded.
- **persist FORWARD now n=34 (Tier1=15, Tier2=7)** (was n=5 on 06-23): separation HOLDS, directional + MONOTONIC — below-T1 avg_roi −0.354 (WR 15.8%) → ALL/LV −0.187 → Tier1 +0.026 → **Tier2 +0.196** (WR 28.6%). BUT **underpowered** — Tier2 CI [−1.0,+1.49], Tier1 [−0.77,+0.92] (both span 0); n=7 Tier2 = 3 trades swing it. Direction pro-persist, significance NOT there. Confirm still = avg_roi CI>0 @ Tier1 30-50 (~1wk) / Tier2 ~30 (~3-4wk).
- **⭐ LIVE-POSITIONS vs RAW-RECONSTRUCTION DIVERGENCE (the load-bearing finding).** Equity $100, bet_fraction 2%, compound, efficient-only, realistic fill:
  - **Live-positions** (285 trades the bot ACTUALLY traded; persist computed on them): LV $79 (−21%) → Tier1 $102 → **Tier2 $135 (+35%)**; persist separates STRONGLY (WR 23→36%, roi −0.05→+0.41).
  - **Raw-reconstruction** (engine replays RAW snapshots → 417 contrarian trades, realistic fill BOTH regimes = the canon negative screen): **EVERYTHING LOSES** — LV $32 (−68%) → Tier1 $59 → **Tier2 $74 (−26%)**; persist BARELY separates (WR 18.3→20.3%, roi all ~−0.19/−0.21, NON-monotonic Tier1≈Tier2). The $ "edge" is **fewer trades (69 vs 235 = less loss-compounding), NOT better selection.**
  - **Reconciliation:** the +35% was **SELECTION-FAVORABLE** — persist correlated with outcome only WITHIN the markets the live bot chose; on the broad reconstructed set it nearly vanishes. Raw-reconstruction = canon negative screen (broader/worse-entry selection + walked fills → pessimistic, understates). Per canon neither certifies; **forward is the authority** (and forward = directional-but-underpowered, above).
  - **Net update:** persist confidence DOWN — the strong in-sample/live separation is partly a live-selection artifact; the broad view is weak. ~30% prior probably a touch HIGH; not dead either (forward direction holds). **Of the 3 hypotheses: #3 (real-but-underpowered) most consistent; #1 (no edge) NOT excluded; #2 (add params / re-calibrate thresholds) = the trap, FORBIDDEN.** Pitfall logged: an equity backtest off RAW must set `starting_balance >> fixed_bet×n_trades` (settlement is post-replay → balance only decrements DURING replay; a small balance starves later trades → false "efficient n=0").
- **LV honest PnL — per-regime FILL COLLAPSE (quantified):** held-to-resolution by regime × fill — revert MID-fill avg_roi **+0.70** → revert REALISTIC **−0.05** (SAME regime, only the fill changes); efficient realistic **−0.05**. → **LV ≈ breakeven-to-slightly-negative in BOTH regimes after realistic fills; the +$9k revert paper-profit is a FILL MIRAGE** (mid 0.25 vs realistic ~0.30 after ask+walk). WR is fill-independent (revert 34% vs efficient 22% = the real regime gap stands).
- **persist cross-regime (in-sample, descriptive):** the FROZEN efficient-calibrated thresholds ALSO separate in REVERT — Tier2 WR ~38% (revert) ≈ ~40% (efficient); below-T1 ~17%/20% (both). Mild-encouraging (generalizes across regimes, not an efficient-only artifact) BUT in-sample + conflicts with the raw-reconstruction's weak separation (selection again). → one threshold set generalizes ⇒ **no per-regime re-calibrate needed (AND it's frozen anyway).**
- **macro-regime — logic CORRECTED:** macro is low-EV REGARDLESS of persist; persist's outcome changes its RELEVANCE (persist works → routed-around/irrelevant; persist fails → only path to the regime edge, but STILL low-EV), NOT its EV. Honest call: **persist fails → STOP (accept efficient), NOT "chase the low-EV macro door."** Supersedes the earlier "revisit if persist fails" framing.
- **Data constraint (re: more-data hopes):** persist is BOOK-based; book depth is off-chain/ephemeral + price-history is EMPTY for resolved → **persist can ONLY grow FORWARD (run the bot); NO historical backfill.** The TAPE (Data-API /trades) IS retroactive (price/regime/wallets, no book) → can help only the parked regime-base-rate question (low-EV, routes-to-breakeven), NOT persist.
- **ML / latent-signal — NOT worth (logged as a Don't):** small predictable residual (efficient) + tiny sample (n=1 regime, ~hundreds of independent markets) → ML = the overfit trap AMPLIFIED (vastly bigger search → more train-winners that die forward); persist caps the real findable edge. Lever = a different market STRUCTURE, not a fancier model.
- **Ops:** contrarian_mv REMOVED from live (`ACTIVE_STRATEGIES=contrarian`; file kept, backtestable). bot_bt.db (2.8GB, regenerable via `make_bt_db`) deleted as a transient — recurring 2.8GB swing, delete after each persist check.

## Session 2026-06-23 — wide-spread × late-entry: NO mispricing (price fair; spread = pure cost)
Read-only test (bot.db, n=576, `research/probe_late_entry_ev.py`): does entering 10-15 min pre-resolve,
filtered to WIDE spread, have +EV? Buy the cheap side at realistic taker fill (cheap + ½-spread). Full
robustness battery (benchmarks / distribution / liquidity buckets / calibration / spread-sweep / regime).
- **@MID (no-cost = pure calibration) ≈ 0 EVERYWHERE** — cheap-side −0.016±0.025; ≈0 across liquidity
  terciles, both regimes, and every prob bucket. Extreme tail p<0.05 (n=255): win-rate 0.016 vs price 0.016,
  **resid −0.001** → perfectly calibrated. **No price-vs-outcome mispricing to harvest.**
- **Spread = a monotonic COST, not a signal:** @fill EV ≥0.005 −0.028 → ≥0.01 −0.036 → ≥0.02 −0.064 →
  ≥0.05 −0.118. Wider = strictly worse (you pay more). Robust to threshold → category (2) liquidity/
  execution, NOT mispricing.
- **Benchmarks all ≈0:** cheap −0.025, favorite +0.007, random −0.009; vs hold(=0), trading is ≤0.
  **Thin ≠ different** (liquidity terciles all ≈0 — not a viable cell within this market).
- **Distribution:** bounded loss (buying); mean (−0.025) pulled UP by tail wins (p90 +0.56), median −0.045
  → mostly small losses + longshot lottery, NOT blow-ups. No hidden +EV in the tails.
- **Verdict: NO +EV.** Extends (does NOT duplicate) "entry-timing refutes enter-near-resolve" (*Session
  2026-06-18 cont.*) + "calibrated, zero taker edge" — now confirmed for the wide-spread/late cell via
  @MID≈0. Pitfall logged: `best_bid`/`best_ask` are RAW per-token (NOT YES-normalized) → use `price` for
  the YES prob + spread-WIDTH (invariant); a naive (bid+ask)/2 mid gave a +0.90 EV artifact.
- **persist forward — FIRST READ (06-23):** rebuilt bot_bt.db + `probe_persist_forward.py`: forward (≥06-22)
  **n=5 Tier1 / n=1 Tier2** (target 30-50) = INSUFFICIENT (leaning −0.235 but CI [−1.0,+1.3] = noise);
  in-sample gradient intact. Recheck ~1-2 wk at n~30-50. FROZEN (no threshold edits).

## Session 2026-06-21 (cont.) — ⭐⭐ PERSIST (depth-support persistence): first candidate to pass the FULL stack
The depth-imbalance candidate done RIGHT (temporal). `persist` = MEAN YES-side depth imbalance
(bid_depth−ask_depth)/(bid+ask) over the 12m PRE-entry window, oriented to the held/fade side. NOTE on
sign: persist is mostly NEGATIVE — at an extreme the book is heavier AGAINST the fade; HIGH persist = LESS
adverse than typical, NOT net-supportive (persist>0 is only ~4% of efficient trades). Ex-ante, no
regime-guess. Labels = LIVE contrarian ledger. The ONLY
signal all session (vs candle ×12, depth d_dimb/stability — all noise) to survive every guard:
- **AUC:** ALL 0.698 / revert 0.661 / **EFFICIENT 0.740** (beats best-of-N each) — strongest in the
  efficient regime, the cell where contrarian otherwise has NO edge.
- **NOT a coin/liquidity proxy (within-coin):** BTC-only AUC 0.672 (n=116), ETH-only 0.739 (n=61) — works
  INSIDE a single coin.
- **Logistic (win ~ persist + price + dimb_now + coin + regime):** persist coef **+1.067, z=2.66, p=0.008**;
  price (p=0.62) AND static depth dimb_now (p=0.95) go INSIGNIFICANT once persist is in → **persist
  SUPERSEDES static depth_ratio** (the snapshot was the proxy; the PERSISTENCE is the signal). +1 SD ≈ 2.9× odds.
- **⭐ PnL after REALISTIC fill (era-2 efficient = honest):** efficient persist tercile avg_roi
  low −0.614 / mid −0.207 / **high +0.501** (monotonic); BTC/ETH-only efficient high = **WR 47.6%, edge
  +21pp, avg_roi +0.715** → the filter turns the efficient regime from a LOSS into a PROFIT.
- Mechanism: persistent DIRECTIONAL book support = real resting order flow (not a transient spoof) → the
  extreme reverts toward the supported side. (User's #1 hypothesis confirmed; ACCELERATION/d_dimb #2 = NOISE.)
- **CAVEATS (binding):** thin n (efficient high-persist ~21-24 trades, ~10 winners → the +0.72 magnitude is
  IMPRECISE, will shrink fwd; edge survives while WR stays >~28%); ONE window (06-16→20 efficient); the
  tercile threshold is IN-SAMPLE (forward needs a FROZEN threshold). NOT forward-confirmed → **strongest
  candidate the project has had, earned a forward paper test, NOT confirmed money.** If it holds forward it
  flips the regime verdict from "lean pivot" to a real efficient-regime filtered edge. **Bootstrap CIs are
  HONEST about the thinness:** only BTC/ETH Tier2 (persist>−0.10, n=15) has an avg_roi CI EXCLUDING 0
  ([+0.23,+2.09]); the broader Tier1 cells SPAN 0 (efficient Tier1 +0.07 [−0.43,+0.65]). WR CIs are tighter
  (BTC/ETH Tier2 60% [36-80], lower bound > entry) — strong direction, in-sample-significant only in the
  tightest cell.
- **NEXT — FROZEN forward test (pre-registered 2026-06-21, NO post-hoc shift — shifting voids it):** persist
  is mostly negative so the cut is RELATIVE — **Tier1 persist>−0.169 (P50 eff), Tier2 persist>−0.100 (P75
  eff)**; forward = trades opened ≥2026-06-22 (efficient era); report n / n_win / WR[Wilson] / edge /
  avg_roi[bootstrap] per tier, NO new threshold. **No live wiring needed** — the bot already logs contrarian
  trades + FULL book depth, so persist is reconstructed offline; measure-only ⇒ does NOT slow the
  depth_ratio research (gating would; deferred until/unless forward confirms). Confirm = forward avg_roi CI
  clearly > 0 at 30-50 high-persist efficient trades. Runner: `research/probe_persist_forward.py` (FROZEN
  constants). Earlier passes: probe_depth_dynamics / probe_depth_persist_confound / probe_depth_persist_pnl.
- **Refinements screened (2026-06-22) → NONE add beyond persist:** order-book RESILIENCY (refill-after-
  depletion), liquidity-VACUUM (depth trough), support-TREND — all NOISE or REDUNDANT (raw AUC < best-of-N
  and/or partial(|persist,price) collapses ~0 / sign-flips). persist is the COMPLETE expression of the
  "order-book real-support" theme → do NOT re-mine these cousins. (Order-flow toxicity = NOT computable: no
  trade-size/volume logged. Cross-coin breadth = the dead cross-coin/beta theme.) One-off screen; probe removed.

## Session 2026-06-21 — candle features as an EX-ANTE FILTER on contrarian: NONE (incl. structure)
DISTINCT from the 06-17/18 STANDALONE candle scan (big-move ≈ candle → price proxy): here candle info is
tested as a FILTER on contrarian's win/loss — ex-ante, regime-split, efficient-focused. Labels = the LIVE
contrarian ledger (clean); features from binance spot ticks BEFORE entry (~10s ticks → tick-derived 1m
OHLC, so wick/structure are COARSE). **volume-spike + VWAP DROPPED** (binance volume not persisted — parser
stores close only). 12 features, 2 clusters. Guards: AUC (no threshold to tune) + per-regime placebo + a
**BEST-OF-N placebo** (the bar the best of 12 must clear) + chrono train/test. Runner:
`research/probe_candle_filter.py`. n = 342 all / 250 revert / 92 efficient.
- **⭐ The 5 STRUCTURE features (wick-rejection, consecutive-exhaustion/concentration, ATR expansion,
  streak, break-of-structure) = ALL NOISE.** None beats best-of-12 (floor: ALL 0.095 / revert 0.111 /
  efficient 0.208) in any regime. BOS — the best a priori ("don't fade a fresh trend-break") — beats
  per-feat p95 in ALL/revert but FAILS best-of-N and FLIPS train→test (efficient 0.380→0.545). The
  best-of-N guard is exactly what exposes these as false positives a per-feature p95 would have passed.
- **The MAGNITUDE/extension cluster (ret_15/30m, dist_MA30, ema20d, vol_30m) is real ONLY in revert** —
  beats best-of-N; AUC<0.5 ⇒ winners had MILDER pre-entry moves ("fade mild, not strong"). But it is ONE
  correlated factor = a PRICE/VOL proxy (same death as the standalone scan), in the regime contrarian
  ALREADY wins (redundant). dist_MA30 strongest (AUC 0.291 revert).
- **⭐ EFFICIENT regime (the target): NOTHING clears the honest bar.** Max efficient AUC dev = vol_30m
  0.149 — beats per-feat p95 (0.147) but FAILS best-of-N (0.208), n=92 thin, and vol_30m ≈ the vol gate
  already used → a proxy re-slice, not new alpha. Its tercile (low-vol WR 35.5% vs high-vol 6.7%) is
  suggestive but underpowered + proxy → joins the efficient-n queue, NOT a confirmed filter.
- **Verdict:** candle info (magnitude OR structure) gives NO independent ex-ante filter — especially not in
  the efficient regime. Contrarian stays regime-conditional; no candle rescue. The only live efficient lever
  remains `depth_ratio` (order-book microstructure — a DIFFERENT data source). CAVEAT: wick/BOS/VWAP deserve
  a real 1m-kline+volume feed for a fully fair test (prior: still noise) → a new data-collection project.

## Session 2026-06-20 — contrarian_mv RE-TESTED: regime-conditional, NOT dead
The prior "MV dead" rested on the EFFICIENT-regime live canary + the fill-DEPENDENT `avg_roi`. Re-ran the
fade family segmented by REGIME on the fill-INDEPENDENT edge (WR − avg_entry). Method fixes vs the earlier
combined backtest: EACH strategy gets its OWN replay (`SimPortfolio.is_held` is per-MARKET, not
per-strategy — a combined run lets whoever triggers first STEAL the market, which is why the earlier LV n
was ~50, now 170); $25 sizing-neutral + huge bank (fixes the ~40-trade balance-depletion artifact); LV =
calibration ANCHOR. Engine: added `SimPosition.decision_price` (held-side mid) for the edge metric.
Runners: `research/probe_mv_retest.py`, `research/probe_winloss_chars.py`.
- **⭐ The backtest is nearly BLIND to the fade edge — read RELATIVE to LV only.** LV-REVERT, whose LIVE
  edge is +15.5pp (z=5.0), scores just **+1.4pp (z=0.5)** in the backtest (engine selects a worse market
  set, WR 18% vs live 24-27%). So the understatement isn't "+0.4 on avg_roi" — the edge nearly VANISHES.
  Absolute backtest numbers are worthless for this edge; only the comparison to the LV anchor is.
- **⭐ MV "dead" was efficient + fill-dependent; segmented it FAILS TO KILL.** MV-REVERT edge **+4.9pp
  (z=2.1, avg_roi −0.036 ≈ breakeven after fills) on BTC/ETH** (+2.3pp all-coin) — ≥ LV-revert in the SAME
  blind lens. MV-EFFICIENT dead (+0.1pp, avg_roi −0.27, matches the live −$1350 @ 9% WR n=23). MV is
  **regime-conditional LIKE LV** (revert +, efficient dead) — the "dead" verdict was the same
  efficient-regime trap as LV's one-window caveat, read on the cost-laden metric.
- **Win/loss characterization (marginal cuts).** ROBUST shared signals (consistent across MV+LV +
  mechanistic): (1) revert > efficient; (2) **fade DIPS wins, fade PUMPS doesn't** (MV +2.8 vs +0.0; LV
  +2.0 vs −0.7) = bull-drift fingerprint, would likely FLIP in a bear window; (3) liquid coins win, thin
  die (BTC + ; XRP/DOGE −3 to −4pp); (4) MV clean-fill(ok) +5.8pp (z=2.6) vs walk −2.4 (adverse selection
  on thin books). INTRIGUING-but-THIN divergence (watch only): MV wins deep-longshot(<.165) +
  short-runway(10-20m); LV wins shallow(.18-.20) + long-runway(30-45m) — opposite → MV may not be a pure
  LV-at-mid_vol duplicate.
- **Discipline.** One window, blind lens, z≤2.6; multi-axis patterns are the project's #1 overfit trap →
  HYPOTHESES for a LIVE read, NOT tuning targets. Negative screen: fails-to-kill ≠ certified alive. The
  ONLY way to close the cell = a live canary (BTC/ETH, SIMPLE/unchanged params, scored on the revert
  subset) through a revert window — left OFF for now (run is contrarian-only for the depth-imbalance n).
- **HV reconfirmed DEAD** (worst/flat in every cut); strategy file removed. The stale `.env.local`
  "contrarian_mv SETTLED DEAD / don't re-enable" note was INACCURATE for the revert cell — corrected.

## Session 2026-06-17/18 — fill-reality + exit/strategy scans (efficient, reconfirmed)
- **⭐ The +$11k contrarian PAPER ledger is MID-FILL INFLATED.** `research/fill_backtest.py` re-priced ALL
  272 contrarian trades at the realistic ask+walk → **−$8,477** (SPREAD-TRUE −8,386 ≈ SPREAD+WALK, so it's
  the spread vs the cheap recorded entry, NOT the depth walk). Date-split: the most "profitable" days
  (06-13/14, +$2.7k/+$4.9k paper) are the MOST negative realistic (−$2.2k/−$2.7k). NOT variant pollution
  (fill_backtest filters `strategy='contrarian'` exactly). The +$11k is era-1 mid-fill; the honest edge =
  the **era-2 subset (recorded realistic fills) only — small, thin, unproven at scale**. The WR edge
  (33.5% vs 20.3% efficient, z=5.6) is real + fill-independent, but at the ASK the cost eats most of it.
- **Runway gate (shipped):** sub-10min contrarian entries were **0/6** (no reversion runway). Tightening
  to >15/20/30m **KILLS WINNERS** (10–20m buckets WR 33–40% = normal-quality) — 10min is the right line.
- **⭐ SL canary SCORED (resolves prior TODO):** 17 `time_gated_sl` closes — **all 17 losers salvaged, 0
  winners killed, net +$100.71** (cuts loss, modest ~$6/close). Patchy by design: catches ~12% of BTC/ETH
  losers (18% had no book event in the 2-min window; rest = no_exit / value never ≤0.10 in time).
- **Exit search (train/test):** spike-TP (cut on a price jump) = DEAD (caps winners, test-negative);
  flat-cut (cut if flat too long) = inert (rarely triggers); a **20-min time-cut** survived ONE split
  (+$469/+$756) but optimistic fill + overlaps slow-rise → unvalidated LEAD, not deployed.
- **⭐ Big strategy scan + ORTHOGONALIZATION:** momentum / mean-rev / big-move (≈ candle; GBM-spot =
  lead-lag, already dead) all null/flip on test. The one CV-stable "survivor" (high-vol momentum +0.152)
  **COLLAPSED under orthogonalization: +0.135 → −0.007 = price proxy** (same death as imbalance).
  "Buy-favorite-at-ask +edge in high-vol" = **DRIFT fingerprint** (one-sided: buy-YES + / buy-NO −, +
  at every level incl underdogs). Only the favorite-longshot bias (price~resid +0.115, 5/5 stable) is a
  REAL miscalibration — but within-spread, untradeable (= the dead momentum). Mid/high-vol fade reconfirmed
  dead (blanket reversion edge −0.015, every depth negative).
- **Direction per-day:** this window is MILD/MIXED (51% YES overall, varying daily, only **2 bear days**),
  NOT strong bull. So apparent edges aren't cleanly "drift", and we LACK a **sustained BEAR regime** — the
  real direction-robustness gap. (Direction = an OFFLINE measurement label, NOT a bot classifier: it's
  unpredictable forward, and no surviving strategy needs it. Don't add trend/liq/bull-bear classify.)
- **slow-rise exit canary ENABLED 2026-06-18** (`SLOWRISE_ENABLED=true`): forward paper test on
  contrarian's held positions (an EXIT — partners with contrarian, doesn't collide; SL = dying ≤0.10,
  slow-rise = weak riser @0.40). Frozen params; score via tagged `slow_rise_exit` (net salvage +
  winners-killed, like the SL) → keep / flip off. **Do NOT tune the knobs off live losses** (overfit).
- **Net:** efficient market reconfirmed across every archetype. ONE real miscalibration (favorite-longshot
  bias), untradeable; everything else noise or price-proxy. Contrarian's honest realistic edge is
  thin/unproven — needs a larger era-2 sample + a 2nd (bear) regime.

## Session 2026-06-18 (cont.) — the 2nd regime ARRIVED; regime is UNDETECTABLE ex-ante
The long-sought non-reverting regime showed up on its own (06-16→18); **contrarian's edge did NOT survive
it.** That makes "switch strategy by regime" the obvious ask, so this session falsified that end-to-end.
All read-only on `data/bot.db` (live ledger, clean labels) unless noted.
- **⭐ 2nd regime, and the edge FLIPPED.** Per-window calibration (longshot = the side contrarian buys;
  edge = WR − avg_entry): **REVERTING 06-11→15 = +15.5pp (n=234, z=5.0)** vs **EFFICIENT 06-16→18 = −5.6pp
  (n=68, z=−1.3 ≈ fair)**. Same params, opposite result → the binding 2nd-regime caveat is now DATA:
  contrarian is +EV reverting, ~0/−EV efficient. Daily edge +16/+17/+17/+15/+0 | −13/−5/+2 (one clean
  transition 06-15→16). Balance peak +$8.9k (06-15) → +$6.6k (06-18).
- **⭐ The regime is HARD to detect at entry (5 separators fail).** _[CORRECTION 2026-06-22: "UNDETECTABLE/
  impossible" was an OVERCLAIM — this is STRONG negative evidence (placebo: best signal +20pp < noise +27pp)
  + an efficient-market prior, NOT a proof of impossibility (you can't prove a negative). The 5 separators
  were all OUTCOME-based (UP-rate/cluster/trailing-WR); a MICROSTRUCTURE-regime signal (aggregate book
  behaviour, e.g. recent persist across markets) is UNTESTED = the one open door, but low-EV. And persist
  already routes around the NEED (regime-AGNOSTIC per-trade filter) → a detector is largely UNNECESSARY.
  Don't chase now; revisit only if persist fails forward.]_ The difference lives ONLY in realized WR
  (post-hoc); every entry-observable feature is identical across the windows: (1) **UP-rate 48.3% vs 52.9%**
  (two-prop z=0.68, NS; daily UP-rate↔edge corr **−0.04**; 06-13 UP58%→+17 vs 06-16 UP59%→−13 = same
  direction, opposite edge → it's calibration-of-extremes, NOT direction); (2) entry price ~identical
  (20.4% vs 22.6%); (3) cluster-size: same buckets, opposite outcome (small clusters +63% ROI reverting /
  −50% efficient); (4) significance only emerges ex-post (needs n~234 for z=5). **Ex-post labeling (what
  every prior verdict did) ≠ ex-ante prediction (what a switch needs)** — I can label the regime AFTER
  resolutions, the bot cannot BEFORE entry.
- **⭐ Trailing-WR regime switch = FAILS (honest no-lookahead sim).** "Pause contrarian after a loss streak
  (last-K WR low)", chronological, trailing computed ONLY over trades resolved before entry. In **every**
  (K,thr) the SKIPPED trades were **profitable** (edge +8…+30pp) — it throws away WINNERS (taken PnL <
  baseline always; K=20/thr30% = −$4.4k vs baseline). Autocorrelation ~nil: after trailing-WR<30% the NEXT
  trades still won 31% (edge +8). In the GOOD block it false-paused **64×** (−$1.6k of winners). Root cause:
  at ~33% WR a 10-trade loss streak is NORMAL binomial noise (P(≤2/10)≈30%) → can't tell variance from a
  regime shift; and only **n=1 transition** exists to calibrate → any (K,thr) overfits.
- **⭐ Signal search + PLACEBO (label-shuffle null).** Looped simple ex-ante filters (coin/side/hour/dow/
  price/runway/trailing-WR + 2-way combos, n≥20) on contrarian resolves. Best REAL edge = **+20pp**
  (coin=BNB&runway>50m). Same search on **300× shuffled labels** → best edge from PURE NOISE: **mean +27,
  p95 +35, max +47pp**. **Real best (+20) < noise mean (+27)** = nothing above chance; best filter halves
  OOS (+24 train→+12 test). Definitive demo of "searching manufactures train-winners" — on THIS data.
- **Entry-timing measured (refutes "enter near resolve").** Contrarian edge GROWS with runway: >50m
  **+18pp**, 40-50m +15, but **≤30m = +0.9pp (≈breakeven)**, 20-30m −8. A fade needs time to revert; near
  resolve the extreme is informative. Entering later = worse, not better (confirms the runway-gate logic).
- **Mirror blend = deterministic bleed (contrarian + momentum can't complement).** Same trigger, opposite
  side = holding BOTH outcomes of a binary = get exactly 1.00 back, pay 1+overround → **−spread/market,
  deterministic, regime-independent** (avg longshot-zone book spread = **1.5¢**). No variance to reduce →
  a portfolio bleed, not a complement; relative-value/cross-coin residual was already NO_SIGNAL
  (`cross_coin.csv`). They coexist only as independent MEASUREMENTS (per-strategy avg_roi), never a hedge.
- **Momentum canary BUILT (`src/strategy/momentum.py`) — follow-the-EXTREME-favorite = mirror of contrarian.**
  PRIOR = DEAD (moderate-favorite momentum was −$187 live; the extreme-favorite "+5.6pp" is the arithmetic
  mirror of contrarian's −5.6pp loss, z=1.3 noise, −15.5pp reverting). A forward paper canary to MEASURE,
  not because edge is expected. `is_held` made **per-(market,strategy)** (was per-market) so it runs ALONGSIDE
  contrarian without colliding; `get_open(strategy=)` + exits scoped to contrarian (`EXIT_STRATEGY`) so
  momentum is held-to-resolution. KILL-LINE: score per-strategy avg_roi at n≥~100, drop if ≤0, no tuning.
  `ACTIVE_STRATEGIES=contrarian,momentum`. Tests: `test_momentum.py`, `test_portfolio_multistrategy.py` (218 green).
- **Net:** the 2nd regime confirms contrarian's edge is regime-conditional AND not switchable (undetectable
  ex-ante; triple-confirmed: placebo < noise, cross_coin NO_SIGNAL, mirror-identity bleed). Within hourly
  crypto Up/Down the search is **EXHAUSTED**. The only remaining lever = a **different market structure**
  (longer timescale / thinner / non-crypto) — a new data-collection project, NOT a recombination here.

## Session 2026-06-19 — microstructure autopsy: depth imbalance = the ONE signal (RESEARCH CANDIDATE)
Winner-vs-loser autopsy on contrarian entries: held-side book AT ENTRY (touch-only; reflect via
`yes_book_from_token` + `infer_outcome`), AUC + permutation + family-wise guard. READ-ONLY on `data/bot.db`.
- **⭐ depth imbalance is the ONLY entry-microstructure signal that PASSES.** Of 9 features (spread, bid/ask
  size, bid/ask depth, depth_ratio, book_age, one-sided, mins-to-resolve), the winner/loser split CLEARS the
  family-wise null: real best |AUC−.5| = **0.182 > shuffled max 0.133** (not a multiple-comparison artifact).
  Carriers: **depth_ratio (held bid_depth/ask_depth) AUC 0.682**, bid_depth 0.659, ask_depth 0.591,
  mins_to_res 0.632 (re-confirms runway). Winners bought a longshot with DEEPER resting bid support (median
  ratio 0.93 vs 0.69) → real bids under it → reverts up more. spread, bid/ask SIZE, one-sided, book_age = NULL.
- **Not a pure clock proxy.** corr(depth_ratio, mins_to_res) = +0.65 (collinear w/ runway) BUT survives WITHIN
  runway buckets (<35m 0.67, 35-50m 0.70, >50m 0.58) → adds beyond the existing runway gate.
- **Regime: SOLID reverting, UNRESOLVED efficient.** revert(≤06-15) AUC 0.666; effic(≥06-16) 0.55–0.66 across
  two constructions on tiny n (**W=15**) → cannot call transfer either way. (Corrects a same-day overclaim that
  it "collapses" efficient — n too small. **Efficient n is THE binding bottleneck, not a verdict.**)
- **2nd-feature search = NOTHING independent.** Dynamic 10-min PRE-entry (ex-ante) features: spread_mean 0.48,
  spread_expansion 0.50, depth_refill/resiliency 0.44, one-sided-persistence 0.50 (no variance — gates avoid
  it), activity 0.45 = all DEAD. Only depth_mean 0.61 / ratio_mean 0.69 carry signal = the SAME depth feature
  (corr +0.52 / +1.0). ONE microstructure lever exists, not a stack of independent signals.
- **⭐ PnL-lift test PASSES (train/test, 300× random 70/30; θ picked on TRAIN only).** OOS mean lift **+0.667
  avg_roi, positive in 100% of splits**; placebo random-cull (same kept frac) = −0.002 (49%). Terciles monotone:
  low-ratio (med .35) avg_roi **−0.38**, mid (.77) +0.38, high (1.00) **+1.05**. Bottom-third bid-support trades
  are NET LOSERS; dropping them is a real, out-of-sample, non-overfit lift (≠ random culling).
- **Status = RESEARCH CANDIDATE, NOT production.** Three gates: (1) reverting-dominated — efficient transfer
  UNPROVEN (grow efficient n via the live run, re-AUC at W≥~40 before trusting); (2) avg_roi levels era-1 mid-
  fill inflated (relative tercile story robust; absolute needs era-2 re-score); (3) partial runway overlap.
  NEXT if efficient n holds the AUC: forward-paper a depth_ratio entry gate (don't wire to production yet).

## Methodology canon (load-bearing — every offline verdict depends on these)
- **⭐ Label corruption + touch-only fix (2026-06-11) — the most important methodology finding.** The
  old recovery rule (decisive last price from ANY event) was **52.9% CORRUPT** vs the Polymarket
  resolution API (n=391) — worse than a coin flip: ~96% of markets' final stored row is a
  `price_change` carrying the changed LEVEL's price (30% are >10c off-touch), so deep 0.0x/0.9x levels
  forged "decisive" labels. **Fix = touch-only decisive rule** (`event_type IN book/last_trade_price`),
  now canon in `recovery.py` + all probe replicas: **100% agreement with API truth on 402 markets**
  (205 labels changed, ALL moved TO truth). Ground truth cached: `research/verify_labels.py` →
  `research/diagnostics/label_truth.csv`. **CONSEQUENCE: every offline verdict produced BEFORE this fix
  is VOID.** The live resolver/ledger was never affected (uses real API resolution) — it was the only
  clean dataset all along, and it always said: calibrated, zero taker edge. (API `end_date_iso` is
  administrative, median 660 min off the true hour — do NOT anchor to it; internal stream timing is correct.)
- **Realistic fill model (`src/execute/fill.py`).** A taker BUY lifts the **ask** and **walks depth**
  (chunks of size-at-best, one spread worse per level); recorded `entry_price` is that effective fill,
  NOT the mid. Same function the backtest uses (one source of truth). Replaced the old "mid + flat 0.03
  buffer" fiction (which turned a paper +$9.1k into a realistic −$4.0k). `SLIPPAGE_BUFFER=0.0`. Two
  guards: **`MAX_BOOK_AGE_SEC=30`** (skip a `price_change`-triggered fill vs a >30s-stale cached book —
  blocked 7 fictional walked fills up to entry 0.77 on BNB) and per-strategy **`entry_ceiling`**
  (contrarian 0.30, a limit price on the walk so a thin longshot can't fill into expensive shares).
- **Backtest = NEGATIVE SCREEN, not positive-confirm (`src/backtest/engine.py`).** Replays through real
  `strategy.evaluate()` + `SimPortfolio` with the realistic fill. **Authoritative for things negative
  ACROSS selection** (killed `contrarian_hv` +$879-flat → −0.25 realistic; mv/hv/momentum die offline
  AND live). **NOT authoritative for the survivor LV**: the replay selects different markets than live's
  real-time/subscribed/stateful selection (era-2: backtest 29 markets @10% WR vs live 25 @24-27%, overlap
  19, side-agree 17/19; the 29/29 side-disagreements are legit extreme-oscillations, 0 logic bugs). So it
  **understates LV — the live forward run is the authority.** Faithful via token-polarity reconstruction
  (`_build_outcome_map`, 0 mixed/0 ambiguous over 2,147 asset_ids) + per-market book cache; keeps ALL
  priced poly events (dropping `price_change` broke selection — 29/116 wrong side). `make_bt_db.py` +
  `market_id` index cut recovery from a 10-min hang to ~30s.
- **Regime + drift caveat.** ALL data is essentially one bull-ish macro-window. A "buy the up-side"
  result is **drift, not edge** (the "drift fingerprint": profit only on the YES/up side). A real
  reversion edge wins BOTH sides. Don't knob-hunt (searching N signals manufactures TRAIN winners that
  flip OOS); don't believe one window; don't tune mid-measurement.

## The survivor — contrarian (low_vol extreme-fade)
Buy the cheap extreme longshot (~0.10–0.20, entry_ceiling 0.30), bet intra-hour reversion; gated on
`low_vol`. Live paper ledger (clean labels by construction).
- **⭐ Era-2 PRE-REGISTERED falsification test = PASS (scored 2026-06-14, n=119).** Registered before
  the data: PASS required BOTH (a) WR > cost-breakeven, (b) NO-side actual > implied (the drift control),
  on hour-clustered units; knobs frozen (any tune voids it). Result: **WR 38.7% (46/119) > breakeven
  20.5%** (exact binomial p=0.000); **profit BOTH-sided** — NO-side 39.1% (25/64) > implied 17.6% AND
  YES-side 38.2% (21/55) > implied 17.2% — which **kills the era-2 drift fingerprint** (era-2 alone was
  YES-only). Spread across **43 distinct hours, 29 win-hours** (not hour-clustered). Survives ex-BNB
  (n=77) and on clean-book BTC (n=37, but BTC edge is entirely NO-side → the YES/extreme_low half is
  softer). Score: `research/score_contrarian_test.py`. **FIRST candidate in the project to survive
  falsification.**
- **Statistical bar = CLEARED.** Full live ledger (n=265): WR 33.6% vs efficient-pricing 20.1% →
  **z=5.6**, both-sided (NO +$7.8k / YES +$3.2k), diversified (BNB 41% / BTC 33% / ETH 23%). Longest
  losing streak = 15 (normal at ~34% WR — small losses, big wins).
- **What PASS does NOT mean:** still **one ~2-day regime** (efficient-market prior says a +21pt both-
  sided longshot gap shouldn't persist — most likely a choppy/mean-reverting regime where intra-hour
  extremes revert, i.e. live overreaction the corrupt offline labels could never measure); the era-2 +$
  is compounding-inflated. **Next gate = a SECOND (non-bull) REGIME**, NOT a knob hunt, NOT live capital.
- **Reversion-RUNWAY gate (2026-06-17).** A fade needs time to revert; entries with ≤10 min left were
  **0/6 (all full-stake losses)** — fading a near-settled price with no runway. Gate: SKIP entry if
  ≤`MIN_RUNWAY_SECONDS=600` to resolve (measured vs `snapshot.ts` → replay-safe). In-sample drops exactly
  those 6 losers, 0 winners. STRATEGY floor, distinct from the global fill-realism `MIN_TIME_TO_RESOLVE_SEC=120`.
  Verified live: post-restart entries all 24–56 min runway. Tests: `test_contrarian_runway.py`.

## Exits (overlays on held positions — `src/execute/exits.py`)
Strategies only ENTER; exits are separate. Both found by autonomous TRAIN/TEST search with honest sell
fills (sell HITS THE BID + walks depth; empty/one-sided late bid → `no_exit` = can't sell → hold).
- **Time-gated SL = VALIDATED, ON.** Sell the held side if value ≤0.10 in the final ≤2 min, **BTC/ETH
  only**. Full rigor passed: train/test, reversed split, **5-fold CV 5/5 positive**, fill-stress to −3¢,
  bootstrap P(>0)=100%, hour-cluster 43+/1−. Effect **+$197 (−2¢ fills) … +$341 (@bid)** ≈ +3.5–6% of
  BTC/ETH hold; acts 55/150 trades, **1 winner killed**. BNB excluded (~30% `no_exit`, fictional salvage).
  Mechanism = **near-mechanical residual salvage** (a clearly-dead longshot's last ~10%), NOT prediction
  — time-gating to the last 2 min is why it barely touches winners. `sl_canary_enabled=True` (kill-switch,
  NOT a tuning knob). The unfixable gap = regime (CV guards param-overfit, not regime-overfit). Runner:
  `research/probe_sl_search.py`. Tests: `test_main_stoploss.py`, `test_portfolio_close.py`.
- **Slow-rise EXIT = CANDIDATE, OFF.** Sell at 0.40 if the open→0.40 climb took >10 min (weak momentum
  reverts). Strongest lever the exit search produced: **+$1,068** (sold-subset WR 25.6% vs 40% implied,
  n=43), train/test +335/+732, **CV 4/5**, fill-stress to −3¢ still +$753, 36 distinct hours,
  independent of entry price. **OFF by default (`slowrise_enabled=False`)** — unlike the SL it caps some
  would-be winners, so leaving it on would contaminate the in-flight contrarian 2nd-regime resolved-WR;
  flip True to start its OWN forward paper test. One regime, found post-hoc → treat like the SL canary:
  forward paper, NOT live, do NOT mine further. Tests: `test_slowrise_exit.py`.
- **Take-profit / any-time stop-loss / trailing / entry-filter = DEAD.** TP loses at every level
  (reaching V is a POSITIVE signal — reached-0.5 won 54.6%, reached-0.7 72.6%; blanket TP caps the rare
  big payouts, −$2.7k…−$3.7k). Any-time stops kill winners: **72.7% of losers dip-then-recover**, so at
  the 0.5 mid a future-loser is indistinguishable from a future-winner. EV-neutral in theory anyway
  (calibrated price ≈ martingale → optional-stopping adds no EV). Only the final ≤2 min @ ≤0.10 is decided
  (→ the SL canary). Runners: `probe_stoploss_path.py`, `probe_lastmin_exit.py`, exit bake-off probes.

## Dead — what was tested and killed (autopsies are the permanent record)
- **Inventory unwind near expiry — NO effect (OPPOSITE), tested 2026-06-23.** Hypothesis: wallets that
  build a big one-sided TAKER position early UNWIND near resolve (small reversal). On-chain tape (Data-API
  `/trades`, works for resolved crypto markets w/ UA header) on 50 markets / 34.8k trades / 1503 big-early
  holders: **65% HOLD to settle**; of the 35% active late only **22.4% reduce (78% ADD)** vs 50% chance →
  they DOUBLE DOWN; market corr(early flow, late flow) **+0.30** + price moves WITH early flow → CONTINUATION,
  opposite of the predicted reversal. Mechanism: binaries SELF-SETTLE (unlike futures) → no forced-unwind
  pressure. Read-only single test, accepted. `research/probe_inventory_unwind.py`.
- **Momentum — NEGATIVE at all costs, DELETED.** Zero-edge favorite-buying (0.60–0.80); the bull
  window's 72.7% WR was drift. Broad loss (74% of trades make 80% of it), worst in `high` zone. Full-
  ledger "flip to the momentum side" = **−$2,556**, negative in every band/symbol. The optimal trending-
  regime taker move is **sit out** (the bot does, via the low_vol gate).
- **contrarian_mv / contrarian_hv — backtest MIRAGE, DELETED.** The 5-way backtest showed fade + across
  vol (low/mid/high +$40/+$351/+$879 at flat slippage), suggesting bigger overshoots revert bigger. But
  flat slippage is a mirage: realistic fill → **−0.36 (mv) / −0.25 (hv)** avg-ROI, and live **0/9 (mv) /
  0/13 (hv)** (entries walked to 0.24–0.29, the fill erosion). A 7-signal OOS search (`probe_hv_signals.py`)
  found NO signal rescues hv — every TRAIN winner flipped NEGATIVE on TEST (textbook overfit). mv had
  weak positive OOS hints (cheap/deep longshots) but live fills erased them. Both REMOVED 2026-06-17.
- **breakout (FOLLOW high_vol extremes) — DEAD −$399, DELETED.** 51.8% WR buying favourites = they don't
  hold. Confirms the pattern: **at extremes the market mean-reverts regardless of vol → FADE wins, FOLLOW
  loses.**
- **All taker signals — DEAD on clean labels.** (1) **Intra-hour overreaction**: looked like price
  overreacts to spot momentum (mirror ±11–14pt at T-45/30), but on clean labels the mirror SIGN-FLIPPED —
  it was a **pull-to-50 label artifact** (half-random labels drag every WR toward 50%). Withdrawn.
  (2) **Order-book imbalance**: first signal to pass the full gate stack (top-of-book corr +0.194,
  p=0.000), but WITHDRAWN — orthogonalization collapsed it (partial(imb | price+ret15) = +0.008,
  p=0.805); depth imbalance IS price (corr 0.970). (3) **Cross-coin BTC→alt lead-lag**: NO_SIGNAL across
  294 markets — the weak cells are window drift via crypto beta, not information. Runners:
  `probe_orderbook_imbalance.py`, `probe_cross_coin.py`, (overreaction probe retired). Loss-structure is
  cleanly calibrated (residual −0.002, p=0.72) → loss is random given price, NOT invertible.
- **Maker / earn-the-spread = DEAD offline (2026-06-17).** Method: resting maker bid scored win-rate
  **conditional on FILL** (captures **adverse selection** — a bid only fills when the offer comes DOWN to
  it = when that side is sinking; "buy at the bid → pocket the half-spread" is the same mid-price mirage,
  it vanishes/flips at the ask). (1) To OPEN the idle mid/high zone — DEAD & trustworthy (densely sampled):
  edge conditional on fill **−4.6pp @0.30 … −18.5pp @0.70** (buy YES), worse for NO. (2) To IMPROVE the
  extreme fade — worse than taker: "join the bid" fills 98% (≈ a taker saving ~2c) and that 2c is eaten by
  adverse selection; posting more aggressively is monotonically worse (ROI/trigger −0.345 → −0.427, never
  beats taker −0.304); offline level understated but the negative slope is robust. (3) Structurally a LIVE
  question (`fill.py` flags resting fills as unmodellable offline) — and the offline signal leans against
  it. **Beats taker in ZERO configs.** Reproducible inline: `recover_resolutions` + `_build_outcome_map` +
  per-market book replay (`min(yes_ask)`/`max(yes_bid)`), conditional-on-fill.

## classify audit
- **price_zone = a REAL probability classifier** (monotonic on 491 markets, Brier@t30=0.131); thresholds
  0.20/0.40/0.60/0.80 CALIBRATED + config-driven + tested (`calibrate_zones.py`). Still TIME-BLIND.
- **volatility** = sound method but DISCARDS direction; config-driven + tested, not data-calibrated.
- **Time:** markets decide in the last ~30 min (Brier 0.217→0.045), but **price already encodes ~98% of
  time info** (zone×phase only +1.5% Brier) → time disambiguates mid-zones + sizing, not a new predictor.
- Deliberately NOT added (HOLD until a strategy needs it): trend/direction detector, time-to-resolution,
  liquidity/spread, order-flow. NB: a **direction detector** is the prerequisite for any regime→strategy
  routing — today only the volatility axis is detected (direction is hindsight-only).

## Depth / spread (post-2026-06-09 restart — two-sided book data is YOUNG, ≤30h)
- **Depth capture verified REAL**, not a bug: one-sided book rows (size on one side only) are a GENUINE
  empty side (0 non-book rows carry depth; ~96% within 15 min of resolution; concentrate in thin
  DOGE/BNB). Handling = **FILTER** one-sided rows (no-quote), don't repair. Runner: `diagnose_oneside_book.py`.
- **Spread is tight** — global median **1.0c** (~80% ≤2c) over 105,799 book spreads; widens by
  symbol/liquidity only (DOGE 6c, BNB 4c vs 1c elsewhere), NOT by zone or time. Runner: `probe_spread_structure.py`.

## Trust level & what's open
- **Labels = API ground truth** (touch-only, 100% on 402 markets) — the strongest foundation the project
  has had. PnL math exact. But verdicts stay **regime-limited (one window)** + **sample-limited
  (throttled stream)**; depth/imbalance carry an extra youth limit (post-restart ≤30h).
- **OPEN — narrowing.** The 2nd regime ARRIVED (06-16→18 EFFICIENT/non-reverting) and **contrarian's edge
  flipped +15.5pp→−5.6pp** (see *Session 2026-06-18 cont.*) → the one-window caveat is now a measured
  regime-conditionality, and the edge is NOT switchable (regime tell strongly RESISTED — placebo-negative +
  efficient-market prior; NOT *proven* impossible, see 06-18-cont CORRECTION; microstructure-regime door
  untested + persist routes around the need).
  Still wanted: a sustained-BEAR window + larger era-2 n. But the live conclusion firms up: **within hourly
  crypto Up/Down the search is exhausted**; the remaining real lever is a **different market structure**
  (longer timescale / thinner / non-crypto) — new data collection, not a recombination here. Going live =
  an unbuilt execution layer + the (now-failing) regime gate (see CLAUDE.md *Status*).
- **Macro-regime test = PARKED (underpowered, n=1 transition); macro is RETROACTIVELY FETCHABLE → NO
  forward logger needed.** Can macro states (BTC-RV / funding / VIX / DXY / US10Y / econ-events) predict
  revert-vs-efficient? NOT testable now: 1 transition → confounded with calendar → ~0 validatable DoF.
  UNLIKE sports (Polymarket history blocked), macro is publicly archived (Yahoo/Binance, years) → fetch
  ON-DEMAND. Retroactive probe (`research/probe_macro_regime.py`) RUN 2026-06-23 on the 06-07→06-23 window:
  macro DID differ (revert→efficient: VIX 19.1→17.1, BTC daily-ret +0.6%→−0.7%, BTC-RV 0.0159→0.0139 —
  higher vol/VIX in revert, directionally plausible) BUT n=1 → unattributable (confounded w/ calendar) →
  confirmed INSUFFICIENT. **Fix = more TRANSITIONS (keep the BOT alive), NOT a macro logger** (`src/macro/`
  forward logger is REDUNDANT); re-run the retro probe as transitions accrue. **Logic CORRECTED 06-24: macro
  is low-EV REGARDLESS of persist** (persist's outcome changes macro's RELEVANCE, not its EV) → **persist
  fails ⇒ STOP (accept efficient market), NOT chase macro.** Macro = a maybe-someday CURIOSITY only IF ≥~10
  transitions ever accrue AND still curious (ONE var = BTC-RV, pre-reg low-RV→revert + placebo, NO mining) —
  NOT a fallback plan. Low-priority — persist routes around needing it.
- **Recovery known limit (documented, not bug):** `round(last_touch)` misfiles ~0.4% of markets whose
  dead token lingers >30 min past `:00`; the recovered OUTCOME stays correct, only the hour label drifts.
  `test_backtest_recovery` asserts <5% miss AND every miss is a >30-min linger.

> (Plain-language mirror `CATATAN_REGIME.md` was removed 2026-06-23 — FINDINGS.md is the sole record.)
