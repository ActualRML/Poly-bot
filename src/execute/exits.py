"""Per-tick EXIT overlays — the one place all early-exit rules live, so they don't
get scattered across the orchestrator.

An "exit overlay" watches a HELD position on each fresh book event and may sell it
EARLY (before resolution) via ``portfolio.close_position`` (honest bid-walk fill).
Strategies (``src/strategy/``) only ENTER (they see one snapshot, return a Decision
for one side); they cannot see positions or close them — that is the orchestrator's
job, and these helpers are it. Both overlays are validated forward-test CANDIDATES,
not proven edges (one regime each — see FINDINGS).

Currently two:
  * ``maybe_stop_loss``       — time-gated SL: sell a near-dead held side (value
                                ≤ ``sl_threshold``) inside the final ``sl_window_sec``,
                                ``sl_symbols`` only. Residual salvage. (FINDINGS
                                *Time-gated stop-loss*.)
  * ``maybe_slow_rise_exit``  — slow-rise exit: sell at ``slowrise_value`` (0.40) iff
                                the climb open→0.40 was SLOW (> ``slowrise_min_sec``),
                                i.e. weak momentum that tends to revert. (FINDINGS
                                *Take-profit & exit bake-off* — the strongest lever
                                the exit search produced, +$1,068, passed train/test,
                                5-fold CV, fill-stress to −3¢, broad.)

Both fire on ``book`` events only, so the trigger value (``snapshot.price``, the
YES-perspective best bid) and the sell book (``latest_book``, just cached from the
same event) come from one fresh snapshot — matching the research that validated them.
"""
from datetime import datetime, timezone

from src.execute.decision import Action, Decision

# Exit overlays (SL / slow-rise) were validated on the contrarian longshot ledger
# ONLY. Scope every position lookup to this strategy so they never touch another
# strategy's positions — e.g. the momentum canary's favorites, which are measured
# hold-to-resolution. (A market may hold one position PER strategy since is_held
# became per-strategy; without this scope get_open could return the wrong side.)
EXIT_STRATEGY = "contrarian"


async def _record_sell(executor, pos, snapshot, value, reason) -> None:
    """Audit the early exit in the decisions table (the executor never trades in
    dry-run; this is the paper trail that an overlay fired)."""
    await executor.execute(
        Decision(
            action=Action.SELL,
            strategy=pos["strategy"],
            side=pos["side"],
            size_usdc=float(pos["size_usdc"]),
            price=value,
            market_id=snapshot.market_id,
            reason=reason,
        ),
        snapshot,
    )


async def maybe_stop_loss(settings, snapshot, market_meta, portfolio, executor, latest_book) -> None:
    """Time-gated stop-loss canary (FINDINGS *Time-gated stop-loss*, validated
    2026-06-15): sell a held side whose value has collapsed to <= ``sl_threshold``
    inside the final ``sl_window_sec``, for ``sl_symbols`` only. Mechanism =
    residual salvage of a near-dead longshot's last ~10% in the final minutes, NOT
    a prediction. Running it forward in paper IS the second-regime test. Cheap-gated:
    the DB is only touched once the symbol + window + value scalar gates all pass."""
    if not settings.sl_canary_enabled or snapshot.event_type != "book":
        return
    if snapshot.market_id is None or snapshot.price is None or not snapshot.symbol:
        return
    if snapshot.symbol.upper() not in {s.upper() for s in settings.sl_symbols}:
        return
    rt = (market_meta.get(snapshot.market_id) or {}).get("resolve_time")
    if rt is None:
        return
    secs_left = (rt - datetime.now(timezone.utc)).total_seconds()
    if not (0 < secs_left <= settings.sl_window_sec):
        return
    # Held-side value <= threshold requires the YES price at an extreme (the held
    # side is YES @<=thr OR NO @>=1-thr). Pre-gate on that BEFORE the DB hit so a
    # mid-priced BTC/ETH market in its last 2 min doesn't query every tick.
    thr = settings.sl_threshold
    if not (snapshot.price <= thr or snapshot.price >= 1.0 - thr):
        return
    pos = await portfolio.get_open(snapshot.market_id, strategy=EXIT_STRATEGY)
    if pos is None:
        return
    value = snapshot.price if pos["side"] == "YES" else 1.0 - snapshot.price
    if value > thr:
        return
    if await portfolio.close_position(pos["id"], latest_book.get(snapshot.market_id),
                                      reason="time_gated_sl"):
        await _record_sell(executor, pos, snapshot, value, "time_gated_sl")


async def maybe_slow_rise_exit(settings, snapshot, market_meta, portfolio, executor,
                               latest_book, seen) -> None:
    """Slow-rise EXIT (FINDINGS *Take-profit & exit bake-off*, validated 2026-06-15):
    sell a held side the FIRST time its value reaches ``slowrise_value`` (0.40) IF the
    climb from open to that first reach took LONGER than ``slowrise_min_sec`` (10 min)
    — a slow/weak rise reverts (the sold subset won only 25.6% vs 40% implied). A fast
    riser (strong momentum) past 0.40 is HELD. No symbol gate (validated on the full
    contrarian ledger).

    ``seen`` (a set of market_ids) makes the decision fire EXACTLY ONCE per position:
    once a held market's value first reaches V we decide sell-or-hold and record it,
    so a fast riser that keeps climbing is never sold later. Reset on restart; bounded
    by the run's market count (tiny). Cheap-gated to a thin price band around V so a
    fast jumper (which we'd hold anyway) is skipped and non-held markets are cheap."""
    if not settings.slowrise_enabled or snapshot.event_type != "book":
        return
    mid = snapshot.market_id
    if mid is None or mid in seen or snapshot.price is None or not snapshot.symbol:
        return
    # Pre-gate: a value just crossing up through V sits in [V, V+band]; for a held YES
    # that's price∈[V,V+band], for a held NO it's price∈[1-V-band, 1-V]. Skips the
    # rest of the range (incl. fast jumpers past the band, which are strong risers we
    # hold regardless) before any DB hit.
    V, band = settings.slowrise_value, 0.05
    if not (V <= snapshot.price <= V + band or 1.0 - V - band <= snapshot.price <= 1.0 - V):
        return
    pos = await portfolio.get_open(mid, strategy=EXIT_STRATEGY)
    if pos is None:
        return
    value = snapshot.price if pos["side"] == "YES" else 1.0 - snapshot.price
    if not (V <= value <= V + band):
        return  # not in the first-reach band (a fast jumper PAST it = strong → hold)
    seen.add(mid)  # first reach of V handled — decide once, never re-trigger
    try:
        open_ts = datetime.fromisoformat(pos["ts"])
    except (TypeError, ValueError):
        return
    if (snapshot.ts - open_ts).total_seconds() <= settings.slowrise_min_sec:
        return  # fast riser (strong momentum) -> hold to resolution
    if await portfolio.close_position(pos["id"], latest_book.get(mid),
                                      reason="slow_rise_exit"):
        await _record_sell(executor, pos, snapshot, value, "slow_rise_exit")
