# FINDINGS_SPORTS.md — sports prediction-market research

> SEPARATE research track from the crypto bot. Crypto verdicts live in **FINDINGS.md**; this file
> = the sports record. Code lives in `src/sports/` (own DB `data/sports.db`, own entry points;
> never touches `src/main.py` or the crypto run). Reuses the crypto REST client read-only.
> Started 2026-06-22 (World Cup 2026 live, resolves ~Jul 19).

## The lead hypothesis
**Favorite-longshot (F-L) bias** = the crypto project's "real-but-within-spread / untradeable"
finding (FINDINGS *favorite-longshot*). In sports prediction markets it is the **most documented
finding** (longshots overpriced, favorites underpriced). Bet: it may be **TRADEABLE on retail-heavy
Polymarket** where it wasn't on crypto — because Polymarket sports differs favorably on three axes
(retail book, far deeper longshots, low vig). Edge type = **bias-harvest / modeling**, NOT crypto
microstructure. The machine + discipline transfer; the signal does not.

## What's built (`src/sports/`)
- `discover.py`   — Gamma discovery of WC markets (volume-paged + WC filter; verified live).
- `main.py` + `schema.py` — forward LOGGER → `data/sports.db` (price + spread + closed, per poll).
- `fl_snapshot.py` — Fase-2 instant snapshot: WC winner overround + F-L edge ESTIMATE (literature).
- `odds_data.py`  — downloads FREE historical results + closing odds (football-data.co.uk; cached).
- `fl_backtest.py` — historical F-L calibration + multi-league comparison (the instant edge screen).
- `model_clv.py`  — rolling-Poisson model (goals + SoT/xG-proxy) vs closing line, OOS CLV backtest.

## Findings so far (2026-06-22)
**1. Polymarket history is BLOCKED → no instant Polymarket backtest.** `GET /prices-history` returns
EMPTY for resolved markets (verified on 8 resolved NBA markets; known GitHub issue). So Polymarket's
own price→outcome can only be gathered FORWARD (the logger). This is why the live logger exists.

**2. Fase-2 snapshot (WC winner, `world-cup-winner`, 50 teams):**
   - **Overround ≈ FAIR: sum(Yes)=1.004 → +0.4%** (negRisk keeps it tight). No big house edge. [assumption-free]
   - **Spreads TIGHT: ~0.1¢ (0.0010) across all bands** — the crypto spread-killer is ABSENT here.
   - F-L *estimate* (literature shrink 0.4-0.6): +edge in the MODERATE band (1-20%), ~0 in the deep
     tail (<1%, spread + tail-risk eat it). **ESTIMATE only — assumes the bias; not Polymarket-confirmed.**
   - Confirming on winner markets is structurally hard: ~15 mid-tier teams, ONE resolution (Jul 19)
     → no statistical power from one tournament. Need broad pooling / many events.

**3. Historical F-L backtest (football-data.co.uk closing odds, instant):**
   - **EPL, 2280 matches / 6 seasons:** F-L WEAK — favorite band [0.55-1.0] +1.8pp, longshot bands
     ~−1pp. **WITH sportsbook vig (~5-7%) back-ROI is negative in every real band** → not tradeable
     on sportsbook. Deep band [0-5%] showed +53.8% ROI but **n=43 = NOISE** (discipline caught it).
   - **Multi-league (6 leagues):** F-L signature (favorite +, longshot −) in **4 of 6** (EPL +1.8,
     League1 +4.0, LaLiga +3.4, SerieA +2.6) but **REVERSED in 2** (Championship −0.3/+1.9, League2 mixed).
     The efficiency-gradient guess (thinner league → bigger F-L) did **NOT** hold cleanly.
   - **Verdict:** F-L is **real but SMALL (1-4pp) + INCONSISTENT across leagues + vig-killed on
     sportsbook.** The favorites-underpriced side is the more consistent direction. Not a money-printer.

**4. Model-vs-CLV (rolling-Poisson, goals + SoT/xG-proxy) = clean NULL.** Team attack/defense from a
rolling window → Poisson H/D/A → bet model-judged +EV at closing odds (OOS test seasons, EV threshold
FIXED not fitted, rolling features past-only). Across ALL 6 leagues: **market log-loss < model (market
sharper everywhere)** and **bet ROI negative (−6.5% to −22%)**. SoT/xG-proxy beat goals (model_ll 0.969
vs 0.993 — literature's xG>goals CONFIRMED, so the model is built right) but **neither beats the line.**
Simple modeling path is OUT. A sophisticated proper-xG model might find the literature's modest 4-15%,
but big effort + uncertain + still sportsbook-not-Polymarket → low-EV revisit.

**=> Both instant sportsbook screens (F-L bias + modeling) come back EFFICIENT.** Same as crypto. The
easy edges aren't here. The only untested live hope is Polymarket-SPECIFIC structure (below), forward-only.

**5. Why Polymarket-WC could still differ favorably (the 3 axes, untested):**
   (a) **retail book** (less sharp than closing lines → bias possibly larger);
   (b) **far deeper longshots** (WC winner 0.05-0.5% vs EPL-match ~5% — F-L is strongest at extremes);
   (c) **LOW vig** (+0.4% overround vs sportsbook 5-7% → a small bias might SURVIVE where vig kills it).
   So the weak sportsbook result does NOT kill the Polymarket-WC hypothesis — but it TEMPERS it.

**6. "Exploit-not-predict" deep loop (2026-06-22) — the WHOLE edge space mapped.** User reframe: find
something exploitable even with mediocre WR. Result:
   - **Sharp-vs-soft / line-shopping** (`sharp_vs_soft.py`, Pinnacle vig-removed = truth): ~NULL.
     Single soft book vs sharp +0.6%, line-shopping (best of many books) +1.7% but CI includes 0,
     early variants negative. Bookmakers are efficient vs each other.
   - **Arbitrage** (Yes+No, cross-market): REAL ($40M/yr extracted) but **HFT-ONLY** — arb lifetime
     down to **2.7s**, 73% captured by sub-100ms bots, 14/20 top wallets are bots. Uncapturable
     without colocated HFT. Dead for us.
   - **⭐ LP / maker REWARDS = the ONE viable non-prediction edge.** Post resting orders near mid →
     daily reward payout (~10% APR stable; $5M/mo incentives Apr-2026; makers zero-fee + 20-25%
     taker-fee rebate). Fits "don't need WR" (yield, not a bet). BUT it's a MARKET-MAKING / EXECUTION
     play: needs the UNBUILT live order layer, inventory + adverse-selection risk, it's competed, and
     feasibility (Indo) applies. NOT a research/paper edge — an infra commitment.
   - **Conclusion:** predictive edges are efficient (every screen); mechanical arb is HFT-only; the
     exploitable non-prediction edge is **market-making for rewards** (a different project: execution,
     not prediction). Paper-research bridge: simulate reward + inventory economics on the logger's
     book data (we capture bid/ask/spread) BEFORE any live build.

**7. LP deep-sim (2026-06-22, `lp_probe.py` + `lp_sim.py`) — LP not capturable from our position.**
   - **price-history WORKS for OPEN markets** (1-min fidelity, ~4400 pts via interval=max) — only
     RESOLVED is empty (finding 1). Unblocks fine-grained volatility/microstructure analysis forward.
   - Reward params: rewardsMaxSpread ~2.5c, rewardsMinSize = 100 shares, 397/429 WC markets rewarded.
     BUT market liquidity is in the MILLIONS ($6-22M).
   - **$100 -> reward ~ $0** (share ~0.0013% -> below the $1/market/day threshold). Sub-scale.
   - **The liquid WC-winner markets are POOR LP targets:** even $100k = ~1.3% share = ~$2/day (~0.7%
     APR) because they're too liquid (share stays tiny). Reward APR is INVERSELY proportional to
     liquidity — the ~10% brochure needs THINNER markets (bigger share), which carry more adverse
     selection. So WC (our focus) is a low-yield LP target even at scale.
   - MM vol-vs-spread (1-min history): looks OK on longshots (vol_15m/spread 0.2-0.5; the 0.001 tick
     spread > the small drift) and marginal on favorites (1.4-2.7). BUT ignores (a) adverse selection
     (informed fills — unmeasurable without the trade tape) and (b) the ELIMINATION JUMP (a longshot
     -> ~0 when knocked out; absent from current pre-elimination data — the real tail risk).
   - **Verdict: LP not viable for us** — $100 = $0 (scale), WC markets low-yield even at scale (too
     liquid), real risk = elimination jumps, + no live MM infra + Indo feasibility gate.

**=> SPORTS EDGE SPACE EXHAUSTED.** Prediction efficient; arbitrage HFT-only; LP real but scale +
infra + feasibility-gated, and the markets we can reach (liquid WC) are low-yield. No edge capturable
from our position (small capital, no live execution, Indo). Same honest end as crypto: liquid
prediction markets are efficient for a small retail player. The research machine + discipline is the
asset; the saved money (not chasing non-edges) is the return.

**8. Price-behavior / anomaly deep-loop (2026-06-22, `anomaly.py` + `anomaly_fade.py` + `anomaly_tod.py`)
— no exploitable anomaly.** Using 1-min price-history (open markets), 45 WC markets, 3 iterations:
   - **Autocorr (reversion/momentum):** weak NEGATIVE (~−0.07 to −0.09) at 1-60 min, strongest in
     longshots (36 mkts). LOOKED like reversion — BUT the `overreact` metric (capture on BIG moves)
     was ~0 with CI spanning 0 → the autocorr is driven by SMALL-move BID-ASK BOUNCE, not big moves.
   - **Fade-after-spread (confirm):** fade top-tercile 15-min moves, 13,275 trades — GROSS reversion
     capture **−0.000008 ±0.000014 (≈0)**, NET after spread **−0.001008** (= −the spread), only 2.2%
     net-positive. => the reversion is microstructure BOUNCE, NOT tradeable.
   - **Time-of-day drift:** 2/24 UTC hours marginally "significant" = exactly multiple-testing noise;
     magnitude (~4e-5) << spread. Only vol-CLUSTERING (match windows), no return drift.
   - **Verdict:** price paths efficient at exploitable scale (reversion, momentum, time-of-day all
     null after microstructure/spread/multiple-testing). Discipline caught the bounce + MT traps.
     Caveat: this tests the ACCESSIBLE anomalies; exotic ones would need the trade tape + still face
     the spread/HFT/scale walls.

**9. Conditional / state-dependent anomaly screen (2026-06-22, `conditional.py`) — NO economically
meaningful conditional anomaly after costs + multiple-testing.** sports.db, ~10h window, 32,797 obs /
891 markets, 15-min forward YES returns conditioned on 27 state-cells across all four foci (price-tier,
liquidity, eff-spread, TTR, imbalance proxy; spread transition widen/narrow; volume-spike toxic proxy;
tier×TTR / tier×vol interactions). Rigor: market-CLUSTERED t, shuffle-placebo max|t| floor (x300) =
2.90, Bonferroni(27) = 3.11.
   - **Statistical:** 5 cells beat the pooled-t floor (espr=tight 4.38, espr=mid 4.04, tier=long 3.60,
     long&dvolspike 3.39, long&ttr=far 3.22) — micro-drift in low-spread / longshot / far-TTR states.
     But CLUSTERED t is marginal (2.4-2.8, mostly below floor) → weak once within-market autocorr is
     accounted for.
   - **Economic: dead by 1-3 orders of magnitude.** Conditional drift = 1-3 bp; effective spread
     (best_ask-best_bid) = 29-1797 bp (0.3%-18%, huge on longshots). net = |drift|-spread is negative
     in EVERY cell. No state's return approaches its transaction cost.
   - Liquidity transition (vacuum/refill via spread-change), toxic-flow (volume-spike): null
     (clustered ns + net hugely negative).
   - **Verdict: no conditional anomaly that is BOTH statistically (post-MT/clustering) AND economically
     (post-spread) meaningful.** The effective spread is the wall; conditioning does not open a gap
     wide enough to clear it. Caveats: ONE 10h window (in-sample, no temporal/regime generalization);
     imbalance + liquidity-transition are PROXIES (sizes not logged); toxic = volume proxy (no trade
     tape). More logger-days would sharpen statistical power but the economic gap (drift << spread) is
     structural and will not close.

**10. Microstructure data ceiling VERIFIED + markout decomposition (2026-06-22, `probe_micro.py`,
`trade_tape.py`, `trade_markout.py`) — the FIRST positive edge: uninformed flow / liquidity-provision,
valid under efficiency, but capture-gated.** (The finding-6/9 "microstructure map" was analytical;
this is the tested version.)
   - **Data ceiling (verified, not assumed):** CLOB `/book` returns FULL depth + SIZES (34 bid / 181
     ask levels); Data-API `/trades` returns the TAPE with `proxyWallet` + side + size + price + ts.
     So true depth-imbalance, queue, participant-segmentation, meta-order, flow-toxicity are all
     RESEARCHABLE directly from the API (Polymarket on-chain => participant data public).
   - **Raw trade-price markout was a BOUNCE confound** (−0.0003 ≈ −half-spread reverting). Fixed with
     the Glosten decomposition vs the 1-min mid (43,421 trades, 15 liquid markets):
       effective half-spread +0.00047 | adverse selection **+0.00001 (~0)** | REALIZED +0.00047.
   - **=> Adverse selection ≈ 0: taker flow is UNINFORMED (recreational).** A maker keeps ~the full
     realized half-spread (+0.047%/fill) net. This is the precondition for an MM edge, and it's the
     first POSITIVE result in the whole arc. **Valid under informational efficiency** (efficient price
     + uninformed flow => LPs earn a liquidity premium; market-ecology, not forecasting).
   - **Caveats (do not over-claim):** CI optimistic (obs not independent; cluster widens it, but the
     effect is large); mid = price-history (possibly stale => adverse maybe under-measured); sample is
     LIQUID-market-dominated (tight 0.001 spread; thin longshots not sampled); **capture != existence**
     — total ∝ fill volume ∝ queue-share ∝ capital ($100 => ~0 fills => ~0 total, same scale wall);
     **slow-maker residual-toxicity risk** (fast bots cream clean flow, leave us the toxic fills);
     needs live maker infra + gas/settlement + inventory + capital-lock; one ~3-day window.
   - **Net:** a real structural edge EXISTS (LP to uninformed flow, adverse~0, +0.047%/fill realized,
     efficiency-compatible). Whether WE capture it = infra + scale + speed-gated. Economic magnitude at
     scale is modest (spread-capture + ~0.7% reward APR on liquid markets); the brochure ~10% needs
     thinner markets + skill. The DEEPER edge would be in less-liquid markets where adverse selection +
     spread are larger — UNTESTED here (low trade volume), the one remaining rigorous extension.

**11. Tune-search overfitting demo (2026-06-23, `sports_tune.py`) — "just tune until it fits" = a
mirage, proven 3 ways.** 226 betting configs (side x implied-prob-band x league) on football closing
odds, chronological train/test. Best in-sample tune (away-longshots, Serie A) = **+28.5% TRAIN ->
-58.0% TEST** (OOS collapse). Placebo best-of-N (shuffled outcomes) = **+414% avg / +505% 95th-pct**
best-train ROI under the NULL (longshot variance inflates the floor) -> the real best is BELOW even
the AVERAGE noise best => real data tunes WORSE than random => zero edge. **Verdict: you can ALWAYS
find a tune that fits TRAIN; it never survives OOS + is pure multiple-testing noise.** Backtest is a
NEGATIVE SCREEN + OOS/placebo validation ONLY, never tune-to-fit. Same mechanism that flipped every
crypto search-edge negative (FINDINGS *Don't*), now demonstrated for sports. Tuning is NOT a path to a
sports predictive edge.

**12. Structural-mispricing search (2026-06-23, `large_trade_impact.py` + basket check) — only the
LP/spread premium survives; transient dislocations are absorbed (liquid) or HFT-captured.** Mechanism-
first (rejected anything statistical-only):
   - **Flow/liquidity dislocation (cat 1/4):** TESTED — top-5% trades (n=2389) move the mid by
     **+0.00000** (impact ~0); small PERMANENT +0.00012 (info), no reverting dislocation. Million-deep
     books ABSORB even large flow => NON-TRADABLE on liquid markets. The mechanism needs SHALLOW books
     (thin markets) — untested, and there flow is more toxic (dislocation may be permanent/informed, not
     temporary => not a mispricing).
   - **Event-driven delay (cat 2):** incorporation is fast (permanent move shows within 1 min in
     price-history; in-play is seconds) => transient, HFT/infra-gated. (Sub-minute test needs event-time
     + book data we lack.)
   - **Cross-market / negRisk inconsistency (cat 3):** clean winner basket sums to ~+0.4% overround,
     intraday drift <~1.5%; arb lifetime 2.7s, HFT-captured ($40M/yr to bots, finding 6) => NON-TRADABLE
     for us.
   - **Spread/depth distortion (cat 5):** the spread is a structural PREMIUM for liquidity provision
     (inventory/settlement/capital-lock risk), adverse selection ~0 => realized +0.047%/fill, PERSISTENT
     => the ONE mechanism-backed, cost-surviving "mispricing" — but it's a risk premium, infra+scale-gated
     (= the LP edge of finding 10), not a free dislocation.
   - **Verdict:** no transient structural mispricing is tradeable for us (absorbed by deep books, or
     HFT-captured). The only real, persistent, mechanism-backed one is the LP/spread premium (capture-
     gated). The single untested cell remains thin-market flow dislocation (shallow book + toxic flow).

**13. Minimal small-k LP viability test (2026-06-23, `minimal_lp_test.py`) — DECISIVE: small-k LP is a
dead end; the thin-market hope is FALSIFIED.** One experiment = one decision (per user's design): 2
buckets (liquid vs thin by touch-depth) x 4 numbers (fill-share k/L, spread/2, adverse markout,
net/fill) at k=$100, 20 LP-able WC markets, ~3-day window.
   - **THIN (touch-depth ~$107):** fill-share 94% (you ARE the book) BUT spread/2 +0.00075 < adverse
     +0.00103 => **net/fill = -0.00034 (NEGATIVE)**. Thin flow is TOXIC (informed) and the spread does
     NOT compensate. The big share is a trap. Thin-market viability = FALSIFIED.
   - **LIQUID (touch-depth ~$5.5k):** net/fill +0.00154 (positive) BUT turnover ~0.1x/day => almost no
     fills (buy-and-hold markets) => EV ~0. FLOW-STARVED.
   - **Verdict: CASE 3 (dead end), dual mechanism** — where you can get filled (thin, 94% share) the
     flow is toxic (net<0); where the flow is cleaner (liquid) you can't get filled (turnover 0.1x).
     No middle region. Decision: do NOT build an LP system for small-k. Robust on direction (net/fill
     sign, turnover) despite one-window/low-power/EV-magnitude caveats.
   - **Closes the LP/execution path for a small-k/slow/Indo actor.** Combined with findings 1-12: NO
     edge (predictive, structural, or liquidity-provision) is capturable from our position. The LP edge
     is real only at scale+infra (finding 10); small-k can't reach it (this finding).

## Methodology canon (transferred from crypto)
- **Instant-historical for "is there an edge"; forward-Polymarket for "tradeable on Polymarket".**
  External free data (results+odds) answers edge-EXISTS in days; Polymarket-specific tradeability
  (vig/retail/fills) only comes forward (history blocked).
- **Negative screen, don't-believe-one-window** — here "window" = one LEAGUE (Championship reversed).
  4-of-6 ≠ a law. Backtest can KILL, can't certify.
- **Discipline catches noise** (the n=43 +53.8% mirage). ML on sports is overfit-heaven; train/test +
  OOS + placebo + Wilson/bootstrap CI are the guardrails (the user's edge over naive modelers).
- **Beating closing lines is hard** (they're well-calibrated; F-L edges are small deviations). The
  modeling path (model > line, CLV) is real but modest (literature ~4-15% best case, often zero).

## Open threads / TODO
1. **Forward Polymarket F-L** — logger accruing; needs broad pooling (beyond WC winner) for n. The
   real Polymarket-tradeability test (the 3 favorable axes). Weeks-to-months.
2. **Extreme-longshot test** — sportsbook OUTRIGHT/winner markets (deep longshots like WC), not just
   match markets, to test F-L at the extreme where it's strongest. (football-data is match-only.)
3. **Model-vs-CLV — DONE 2026-06-22 = NULL** (finding 4; market sharper + ROI negative in all 6 leagues).
   Only revisit = a sophisticated proper-xG (shot-location) model — low-EV (modest-at-best, sportsbook≠Polymarket).
4. **Feasibility (go-live)** — Polymarket+sports from Indo = ToS/VPN + gambling-law risk, WORSE than
   crypto. Gate go-live on this BEFORE building any live execution.
5. **LP/market-making rewards paper-sim — DONE 2026-06-22 = NOT viable for us** (finding 7). $100 = $0
   (sub-scale), liquid WC markets low-yield even at scale, real risk = elimination jumps, infra +
   feasibility gates. Only revisit if capital ($thousands) + live MM infra + a THINNER-market focus +
   feasibility all change. price-history-for-open-markets (1-min) is now available for any deeper dive.

## Don't
- **Don't believe one league** (Championship reversed; 4/6 is not a law — could be noise).
- **Don't expect a big edge** — F-L is small (1-4pp) + inconsistent + (on sportsbook) vig-killed.
- **Don't trust small-n bands** (the n=43 +53.8% was noise).
- **Don't use structural country features** (football culture / economy / gov support) — public + slow
  → already priced into FIFA ranking + market odds. Edge (if any) is in match-dynamics the odds
  underweight, not country-facts.
- **Don't conflate sportsbook with Polymarket** — the calibration BIAS shape may transfer; the
  magnitude + tradeability (vig/retail/fills) is Polymarket-specific and forward-only.
- **Don't chase arbitrage** — real but HFT-only (2.7s lifetime, sub-100ms bots). You cannot compete.
- **Don't expect LP rewards to be free money** — the ~10% APR is net of adverse selection + inventory
  risk + competition; it's a market-making craft requiring live infra, not a paper edge.
- **Don't LP the liquid marquee markets** — reward APR is INVERSELY proportional to liquidity; $100k on
  a $7M market ~ 0.7% APR. LP yield lives in THIN markets (bigger share) — which carry the worse risk.
- **Don't trust longshot 'low volatility'** — the daily drift is small but the ELIMINATION JUMP (-> 0)
  is the real LP risk, and it's absent from pre-knockout data.
