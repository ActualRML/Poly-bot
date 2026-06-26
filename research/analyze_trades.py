"""READ-ONLY trade analysis: did losing trades ever go into profit first?

For every position it joins positions x snapshots on market_id over the window
[entry ts, resolved ts (or now)], converts each snapshot's YES-normalized price
to the HELD side, and walks the unrealized-pnl path using the SAME convention as
the resolver / check_state (slippage-adjusted effective entry). From that path:

    MFE   = peak unrealized profit reached during the trade ("sempat naik")
    MAE   = deepest unrealized loss reached
    peak@ = minutes from entry to the MFE point
    n     = snapshots found in the window (0 => no data; flagged + excluded)

READ-ONLY: opens data/bot.db with mode=ro and never writes. Run manually:

    uv run python research/analyze_trades.py
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# --- make `import src.*` resolve regardless of the current working directory ---
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.execute.portfolio import SLIPPAGE_BUFFER  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "bot.db"


class TradeStat(NamedTuple):
    pos: sqlite3.Row
    mfe: float | None          # peak unrealized profit ($)
    mae: float | None          # deepest unrealized loss ($)
    mins_to_peak: float | None
    n: int                     # snapshots in window
    final: float | None        # realized pnl (closed) or last unrealized (open)


def _money(v) -> str:
    return f"{v:+.2f}" if v is not None else "n/a"


def _held_price(yes_price: float, side: str) -> float:
    """snapshots.price is YES-perspective; flip it for a NO position."""
    return yes_price if side == "YES" else 1.0 - yes_price


def _unreal(held: float, entry: float, size: float) -> float:
    """Unrealized pnl on the held side — mirrors resolver/check_state."""
    eff = min(entry + SLIPPAGE_BUFFER, 1.0)
    return (size / eff) * held - size if eff else 0.0


def _excursions(conn, pos, now_iso: str) -> TradeStat:
    """Walk the intra-trade price path and extract MFE/MAE for one position."""
    end = pos["resolved_ts"] or now_iso
    rows = conn.execute(
        """
        SELECT ts, price FROM snapshots
         WHERE market_id = ? AND price IS NOT NULL AND ts BETWEEN ? AND ?
         ORDER BY ts
        """,
        (pos["market_id"], pos["ts"], end),
    ).fetchall()

    final = pos["pnl_usdc"] if pos["status"] != "open" else None
    if not rows:
        return TradeStat(pos, None, None, None, 0, final)

    entry = float(pos["entry_price"])
    size = float(pos["size_usdc"])
    side = pos["side"]

    mfe = mae = None
    peak_ts = None
    last_pnl = None
    for r in rows:
        pnl = _unreal(_held_price(float(r["price"]), side), entry, size)
        last_pnl = pnl
        if mfe is None or pnl > mfe:
            mfe, peak_ts = pnl, r["ts"]
        if mae is None or pnl < mae:
            mae = pnl

    mins_to_peak = None
    try:
        mins_to_peak = (
            datetime.fromisoformat(peak_ts) - datetime.fromisoformat(pos["ts"])
        ).total_seconds() / 60
    except (ValueError, TypeError):
        pass

    # Open trades have no realized pnl yet — show the latest unrealized instead.
    if pos["status"] == "open":
        final = last_pnl
    return TradeStat(pos, mfe, mae, mins_to_peak, len(rows), final)


def _print_table(stats: list[TradeStat]) -> None:
    print(f"=== TRADES ({len(stats)}) ===")
    print(
        f"  {'symbol':<8} {'side':<4} {'strategy':<12} {'status':<9} "
        f"{'entry':>6} {'final':>9} {'MFE':>9} {'MAE':>9} {'peak@':>7} {'n':>5}"
    )
    for s in stats:
        p = s.pos
        peak_s = f"{s.mins_to_peak:.0f}m" if s.mins_to_peak is not None else "-"
        print(
            f"  {str(p['symbol'] or '-'):<8} {str(p['side'] or '-'):<4} "
            f"{str(p['strategy'] or '-'):<12} {str(p['status']):<9} "
            f"{float(p['entry_price']):>6.3f} {_money(s.final):>9} "
            f"{_money(s.mfe):>9} {_money(s.mae):>9} {peak_s:>7} {s.n:>5}"
        )
    print()


def _avg(values) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _print_summary(stats: list[TradeStat]) -> None:
    print("=== SUMMARY ===")
    have = [s for s in stats if s.n > 0]
    no_data = [s for s in stats if s.n == 0]
    print(f"  trades: {len(stats)}  (with snapshot data: {len(have)}, no data: {len(no_data)})")

    resolved = [s for s in have if s.pos["status"] == "resolved"]
    losers = [s for s in resolved if s.final is not None and s.final < 0]
    winners = [s for s in resolved if s.final is not None and s.final > 0]
    print(f"  resolved: {len(resolved)}  (winners {len(winners)}, losers {len(losers)})")

    if losers:
        ever_profit = [s for s in losers if s.mfe is not None and s.mfe > 0]
        pct = len(ever_profit) / len(losers) * 100
        print(f"  losers yang SEMPAT profit (MFE>0): {len(ever_profit)}/{len(losers)} ({pct:.0f}%)")
        if ever_profit:
            avg_peak = _avg(s.mfe for s in ever_profit)
            avg_min = _avg(s.mins_to_peak for s in ever_profit)
            avg_min_s = f"{avg_min:.0f}m" if avg_min is not None else "n/a"
            print(f"    rata-rata puncak (MFE): {avg_peak:+.2f}   rata-rata waktu ke puncak: {avg_min_s}")
        avg_mae = _avg(s.mae for s in losers)
        if avg_mae is not None:
            print(f"    rata-rata MAE loser: {avg_mae:+.2f}")

    if winners:
        avg_mae = _avg(s.mae for s in winners)
        if avg_mae is not None:
            print(f"  winners - rata-rata MAE (drawdown terdalam yang dilewati): {avg_mae:+.2f}")

    if no_data:
        print(
            f"  [!] {len(no_data)} trade tanpa snapshot di window "
            f"(kemungkinan sebelum data dikumpulkan / market tak ter-subscribe)."
        )


def analyze() -> None:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    positions = conn.execute(
        """
        SELECT id, market_id, symbol, side, entry_price, size_usdc,
               status, exit_price, pnl_usdc, strategy, ts, resolved_ts
          FROM positions
         ORDER BY ts
        """
    ).fetchall()

    if not positions:
        print("(no positions yet)")
        conn.close()
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    stats = [_excursions(conn, p, now_iso) for p in positions]
    conn.close()

    _print_table(stats)
    _print_summary(stats)


if __name__ == "__main__":
    analyze()
