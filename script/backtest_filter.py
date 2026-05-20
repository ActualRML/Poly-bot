"""
Filter Diagnostic Backtest — replay historical hourly trades through the scout
pipeline (no short-circuit) to identify which filters are useful vs overkill.

For each closed updown_hourly trade, a ScoutContext is reconstructed from DB
columns and run through evaluate_entry_full(). Per filter we count how often it
fired (failed) on winners vs losers — a filter that blocks losers is valuable,
one that blocks winners is overkill.

Diagnostic tool only — stdout, no file writes, no production-path changes.

Usage: python script/backtest_filter.py
"""
from __future__ import annotations

import io
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.models.database import get_conn
from src.scout.context import ScoutContext
from src.scout.scout import evaluate_entry_full
from src.risk.kelly import KellySizer
from src.execute.exit import ExitEvaluator
from src.execute.position import PositionManager
from src.utils.parsing import detect_symbol_from_question


class _StubGamma:
    """Offline stand-in for GammaClient. extract_token_ids returns []; in
    DRY_RUN, TokenIdFilter still passes (empty token_id only fails in LIVE)."""

    def extract_token_ids(self, market):
        return []


# Context fields with no DB column — reconstructed with a default. Reported so
# the reader knows which filter results may be biased by reconstruction.
DEFAULTED_FIELDS = [
    "vol_annual = 0.40 (no per-symbol realized vol stored) -> biases MinMomentum threshold",
    "buy_winrate = 0.33 (momentum-mode fallback; not stored) -> biases SizingFilter EV",
    "market_regime = None (cross-asset regime not stored) -> SizingFilter vol-scale skipped",
    "btc_scalp = None (scalp signal not stored) -> SizingFilter scalp mult = 1.0",
    "market_session = US_MAIN (session not stored) -> SizingFilter session cap",
    "market volume = 5000 (24h volume not stored) -> avoids false ILLIQUID in MarketState",
    "closed_this_cycle / profit_locked_markets = empty (cycle state not stored)",
    "slot_open_count / slot_history_count = 0 (slot state not stored)",
    "sym_m15m sign normalized to actual trade direction so DirectionalDecision "
    "reproduces the recorded entry (magnitude preserved; abs-based filters unaffected)",
]

# IDLE filters: why they never fire in an offline replay of closed trades.
IDLE_NOTES = {
    "already_closed":      "cycle state empty in replay",
    "profit_locked":       "cycle state empty in replay",
    "slot_open_cap":       "slot count defaulted to 0",
    "slot_cumulative_cap": "slot count defaulted to 0",
    "symbol_blacklist":    "blacklist state not reconstructed",
    "circuit_breaker":     "CB_ENABLED=False (paper phase)",
    "token_id":            "DRY_RUN bypass — not evaluable offline",
    "liquidity_check":     "DRY_RUN bypass — not evaluable offline",
    "can_open":            "all replayed trades closed — no portfolio contention",
}


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _display_name(snake: str) -> str:
    return "".join(w.capitalize() for w in snake.split("_"))


def load_hourly_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM positions
               WHERE status = 'closed'
                 AND strategy_mode LIKE 'updown_hourly%'
                 AND strategy_mode != 'updown_candle_dry_run'
               ORDER BY entry_time"""
        ).fetchall()
    return [dict(r) for r in rows]


def reconstruct_ctx(row: dict, sizer: KellySizer, manager: PositionManager) -> ScoutContext:
    symbol      = detect_symbol_from_question(row["question"])
    outcome     = row["outcome"]
    entry_price = float(row["entry_price"])
    resolve_date = _parse_dt(row["resolve_date"])
    entry_time   = _parse_dt(row["entry_time"])

    # time-to-resolve at entry — delta_sec is a static field; t_min reads it directly
    delta_sec = max((resolve_date - entry_time).total_seconds(), 1.0)

    # hourly candle opens 1h before resolve; rebuild start_date relative to now()
    # so the candle_running_min property reproduces the entry-time value
    candle_start    = resolve_date - timedelta(hours=1)
    running_at_entry = max((entry_time - candle_start).total_seconds(), 0.0)
    synth_start     = datetime.now(timezone.utc) - timedelta(seconds=running_at_entry)

    # market_price_up + m_15m sign chosen so DirectionalDecisionFilter reproduces
    # the recorded (outcome, entry_price)
    market_price_up = entry_price if outcome == "Up" else round(1.0 - entry_price, 4)
    m15_mag = abs(row["sym_m15m"] or 0.0)
    m15     = m15_mag if outcome == "Up" else -m15_mag

    sym_mtf = {
        "m_5m":           row["sym_m5m"] or 0.0,
        "m_15m":          m15,
        "m_30m":          row["sym_m30m"] or 0.0,
        "vol_ratio":      row["vol_ratio"] or 0.0,
        "all_tf_aligned": bool(row["mtf_aligned"]),
    }
    btc_m15m = row["btc_m15m"]
    btc_mtf  = {"m_15m": btc_m15m} if btc_m15m is not None else None

    ctx = ScoutContext(
        market          = {"_symbol": symbol, "conditionId": row["condition_id"],
                           "volume": 5000.0},
        symbol          = symbol,
        condition_id    = row["condition_id"],
        question        = row["question"],
        market_price_up = market_price_up,
        start_date      = synth_start,
        end_date        = resolve_date,
        delta_sec       = delta_sec,
        sym_mtf         = sym_mtf,
        vol_annual      = 0.40,
        vol_data        = {"DEFAULT": 0.40},
        market_regime   = None,
        btc_scalp       = None,
        market_session  = "US_MAIN",
        btc_mtf         = btc_mtf,
        closed_this_cycle     = set(),
        profit_locked_markets = {},
        slot_open_count       = 0,
        slot_history_count    = 0,
        session         = None,
        capital         = 120.0,
        buy_winrate     = 0.33,
    )
    ctx.sizer   = sizer
    ctx.gamma   = _StubGamma()
    ctx.clob    = None
    ctx.manager = manager
    ctx.breaker = None
    return ctx


def classify(s: dict) -> str:
    if s["fired"] == 0:
        return "IDLE"
    if s["on_loss"] > s["on_win"]:
        return "GOOD"
    if s["on_loss"] == s["on_win"]:
        return "NEUTRAL"
    return "OVERKILL"


def quality_label(name: str, s: dict, cat: str) -> str:
    if cat == "IDLE":
        return f"IDLE ({IDLE_NOTES.get(name, 'never fires')})"
    small = " small-sample" if s["fired"] < 5 else ""
    if cat == "GOOD":
        return f"GOOD ({s['on_loss']}L/{s['on_win']}W blocked{small})"
    if cat == "NEUTRAL":
        return f"NEUTRAL ({s['on_loss']}L/{s['on_win']}W blocked{small})"
    return f"OVERKILL ({s['on_loss']}L/{s['on_win']}W blocked{small})"


def main() -> None:
    trades = load_hourly_trades()
    if not trades:
        print("No closed updown_hourly trades found in data/bot_database.db")
        return

    sizer = KellySizer(
        kelly_multiplier=0.5, max_fraction=0.30,
        min_bet_usdc=5.0, min_winrate=0.52,
    )
    manager = PositionManager(
        max_open_positions=5, max_capital_per_market=30.0,
        max_same_direction=2, exit_evaluator=ExitEvaluator(),
    )

    wins = losses = 0
    stats: dict[str, dict] = {}
    order: list[str] = []

    for row in trades:
        is_win = float(row["pnl_usdc"]) > 0
        wins += int(is_win)
        losses += int(not is_win)

        ctx = reconstruct_ctx(row, sizer, manager)
        decision = evaluate_entry_full(ctx)

        for name, res in decision.breakdown.items():
            if name not in stats:
                stats[name] = {"fired": 0, "on_win": 0, "on_loss": 0, "passed": 0}
                order.append(name)
            s = stats[name]
            if res.passed:
                s["passed"] += 1
            else:
                s["fired"] += 1
                s["on_win" if is_win else "on_loss"] += 1

    n = len(trades)
    for name, s in stats.items():
        assert s["fired"] + s["passed"] == n, f"tally mismatch for {name}"
        assert s["on_win"] + s["on_loss"] == s["fired"], f"win/loss mismatch for {name}"

    cats = {name: classify(stats[name]) for name in order}

    print("FILTER DIAGNOSTIC BACKTEST")
    print("==========================")
    print(f"Trades replayed: {n} ({wins} wins, {losses} losses)")
    print(f"Pipeline filters: {len(order)}")
    print("Reconstructed fields with defaults:")
    for fld in DEFAULTED_FIELDS:
        print(f"  - {fld}")
    print()
    print("PER-FILTER BEHAVIOR")
    print("-------------------")
    hdr = (f"{'Filter':<22}| {'Fired':>5} | {'OnWin':>5} | {'OnLoss':>6} "
           f"| {'Pass':>4} | Signal Quality")
    print(hdr)
    print("-" * 22 + "|" + "-" * 7 + "|" + "-" * 7 + "|" + "-" * 8
          + "|" + "-" * 6 + "|" + "-" * 30)
    for name in order:
        s = stats[name]
        print(f"{_display_name(name):<22}| {s['fired']:>5} | {s['on_win']:>5} "
              f"| {s['on_loss']:>6} | {s['passed']:>4} | "
              f"{quality_label(name, s, cats[name])}")
    print()
    print("Legend: OnWin/OnLoss = trades the filter blocked that won / lost.")
    print("  GOOD = blocks more losers than winners | OVERKILL = the reverse")
    print("  NEUTRAL = equal | IDLE = never fired in this sample")
    print(f"Totals verified: fired + pass = {n} for every filter.")
    print()

    overkill = [_display_name(x) for x in order if cats[x] == "OVERKILL"]
    good     = [_display_name(x) for x in order if cats[x] == "GOOD"]
    neutral  = [_display_name(x) for x in order if cats[x] == "NEUTRAL"]
    idle     = [_display_name(x) for x in order if cats[x] == "IDLE"]

    print(f"RECOMMENDED ACTIONS (based on this sample, n={n})")
    print("=" * 50)
    print(f"LOOSEN or INVESTIGATE: {', '.join(overkill) or '(none)'}")
    print(f"KEEP AS-IS (well-calibrated): {', '.join(good) or '(none)'}")
    print(f"NEUTRAL — equal block, need more data: {', '.join(neutral) or '(none)'}")
    print(f"IDLE — no data either way: {', '.join(idle) or '(none)'}")
    max_fired = max((stats[x]["fired"] for x in order), default=0)
    if max_fired < 5:
        print("SMALL SAMPLE WARNING: every filter fired <5 times "
              "-> directional only, not statistically significant")
    else:
        print("SMALL SAMPLE WARNING: most counts are low "
              "-> directional only, not statistically significant")
    print()
    print("LIMITATIONS:")
    print(f"- Sample size: {n} trades ({wins}W/{losses}L)")
    print("- Only tests trades the bot ACTUALLY entered (cannot evaluate filters")
    print("  on trades the old pipeline already blocked — survivorship bias)")
    print("- Some context fields reconstructed with defaults (listed above) — may bias results")
    print("- Polymarket exit prices used as ground truth (pre-resolution dynamics not simulated)")


if __name__ == "__main__":
    main()
