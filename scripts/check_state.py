"""Read-only dump of the simulated trading state in data/bot.db.

Open positions show UNREALIZED pnl computed from the held token's CURRENT CLOB
price, so the script hits the network when there are open positions (it's no
longer fully offline). It still never writes — DB is opened mode=ro.
"""
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# scripts/ is not a package and src/ is not installed (pyproject: package = false),
# so put the project root on sys.path to reuse the bot's API client + config.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.polymarket import PolymarketREST, _normalize_outcome  # noqa: E402
from src.config import Settings  # noqa: E402
from src.execute.portfolio import SLIPPAGE_BUFFER  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"
STARTING_BALANCE = 1000.0
RESOLVED_SHOW_LAST = 30  # only the most recent N resolved rows are printed

# Friendly per-regime labels for the contrarian fade family (low/mid/high vol).
_STRAT_LABEL = {
    "contrarian": "contrarian-LV",
    "contrarian_mv": "contrarian-MV",
    "contrarian_hv": "contrarian-HV",
}


def _slabel(s) -> str:
    return _STRAT_LABEL.get(s, s or "-")


async def _fetch_open_live(open_rows) -> dict[int, tuple[float | None, float | None]]:
    """For each OPEN position, fetch the held token's current price from the same
    CLOB /markets/{condition_id} endpoint the resolver uses, and compute unrealized
    pnl matching the resolver's payout/cost convention (see portfolio.resolve_position).

    Returns {position_id: (live_price, unrealized_pnl)}. Either value is None when
    the live price is unavailable, so the caller shows '-'/'n/a' for that row
    instead of crashing the whole dump.
    """
    s = Settings()
    out: dict[int, tuple[float | None, float | None]] = {}

    async with PolymarketREST(s.polymarket_gamma_url, s.polymarket_clob_url) as api:
        async def one(r) -> None:
            try:
                m = await api._get(f"{api.clob_url}/markets/{r['market_id']}")
                tokens = (m.get("tokens") or []) if isinstance(m, dict) else []
                price = None
                for t in tokens:
                    if isinstance(t, dict) and _normalize_outcome(t.get("outcome", "")) == r["side"]:
                        p = t.get("price")
                        price = float(p) if p is not None else None
                        break
                if price is None:
                    out[r["id"]] = (None, None)
                    return
                # eff_entry mirrors the slippage the resolver assumes on a winning
                # fill, so live_price->1 lands on the resolver's win pnl and ->0 on -size.
                entry = float(r["entry_price"])
                size = float(r["size_usdc"])
                eff = min(entry + SLIPPAGE_BUFFER, 1.0)
                pnl = (size / eff) * price - size if eff else None
                out[r["id"]] = (price, pnl)
            except Exception:
                out[r["id"]] = (None, None)  # fail soft per-row; never crash the dump

        await asyncio.gather(*(one(r) for r in open_rows))
    return out


def _fmt_price(v) -> str:
    return f"{v:.3f}" if v is not None else "-"


def _fmt_resolves(rt) -> str:
    """Minutes left until the market resolves, from the stored ISO resolve_time."""
    if not rt:
        return "-"
    try:
        mins = (datetime.fromisoformat(rt) - datetime.now(timezone.utc)).total_seconds() / 60
    except (ValueError, TypeError):
        return "-"
    return "due" if mins <= 0 else f"{mins:.0f}m"


def _print_open(rows, live) -> None:
    """OPEN positions: entry vs current (live) price, unrealized pnl, time left."""
    print(f"=== OPEN ({len(rows)}) ===")
    if not rows:
        print("  (none)")
        print()
        return
    print(
        f"  {'symbol':<8} {'side':<4} {'entry':>7} "
        f"{'now':>7} {'size':>8} {'unreal':>10} {'resolves':>9}"
    )
    for p in rows:
        price, pnl = live.get(p["id"], (None, None))
        entry = p["entry_price"] if p["entry_price"] is not None else 0.0
        size = p["size_usdc"] if p["size_usdc"] is not None else 0.0
        # '~' marks the figure as an unrealized estimate, not a realized number.
        unreal = f"~{pnl:+.2f}" if pnl is not None else "n/a"
        print(
            f"  {str(p['symbol'] or '-'):<8} {str(p['side'] or '-'):<4} "
            f"{entry:>7.3f} "
            f"{_fmt_price(price):>7} {size:>8.2f} {unreal:>10} "
            f"{_fmt_resolves(p['resolve_time']):>9}"
        )
    print()


def _exit_label(p) -> str:
    """How the position LEFT: held to resolution, or closed early by which exit
    overlay. All rows are strategy=contrarian; this is the entry-vs-exit distinction."""
    if p["status"] == "resolved":
        return "hold"
    cr = p["closed_reason"] or "closed"
    return {"time_gated_sl": "SL", "slow_rise_exit": "slowrise"}.get(cr, cr)


def _print_resolved(rows) -> None:
    """RESOLVED/CLOSED positions: entry, exit, pnl, + how it left (most recent rows only)."""
    shown = rows[-RESOLVED_SHOW_LAST:]
    title = f"last {len(shown)} of {len(rows)}" if len(rows) > len(shown) else f"{len(rows)}"
    print(f"=== RESOLVED ({title}) ===")
    if not rows:
        print("  (none)")
        print()
        return
    print(
        f"  {'symbol':<8} {'side':<4} {'entry':>7} "
        f"{'exit':>7} {'size':>8} {'pnl':>10}  {'via':<8}"
    )
    for p in shown:
        entry = p["entry_price"] if p["entry_price"] is not None else 0.0
        size = p["size_usdc"] if p["size_usdc"] is not None else 0.0
        pnl_s = f"{p['pnl_usdc']:+.2f}" if p["pnl_usdc"] is not None else "-"
        print(
            f"  {str(p['symbol'] or '-'):<8} {str(p['side'] or '-'):<4} "
            f"{entry:>7.3f} "
            f"{_fmt_price(p['exit_price']):>7} {size:>8.2f} {pnl_s:>10}  {_exit_label(p):<8}"
        )
    print()


def _dump() -> None:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Balance
    bal_row = cur.execute("SELECT * FROM balance WHERE id = 1").fetchone()
    balance = float(bal_row["balance_usdc"]) if bal_row else 0.0
    print("=== BALANCE ===")
    print(f"  balance_usdc: {balance:.2f}")
    print()

    # 2. Positions
    positions = cur.execute(
        """
        SELECT id, market_id, symbol, side, entry_price, size_usdc,
               status, exit_price, pnl_usdc, strategy, resolve_time, closed_reason
          FROM positions
         ORDER BY ts
        """
    ).fetchall()

    # Split by status so each table shows only the columns that matter.
    # Void rows are excluded from display (still counted in BY STATUS below).
    open_rows = [p for p in positions if p["status"] == "open"]
    resolved_rows = [p for p in positions if p["status"] not in ("open", "void")]

    # Open rows get a live CLOB price + unrealized pnl (one network round-trip
    # each, run concurrently). market_id drives the lookup but isn't printed.
    live = asyncio.run(_fetch_open_live(open_rows)) if open_rows else {}
    _print_open(open_rows, live)
    _print_resolved(resolved_rows)

    # 3. Summary by status
    summary = cur.execute(
        """
        SELECT status,
               COUNT(*)                        AS n,
               COALESCE(SUM(pnl_usdc), 0.0)    AS total_pnl,
               COALESCE(SUM(pnl_usdc > 0), 0)  AS wins
          FROM positions
         GROUP BY status
         ORDER BY status
        """
    ).fetchall()
    print("=== BY STATUS ===")
    for s in summary:
        line = f"  {s['status']:<9} count={s['n']:<4} total_pnl={s['total_pnl']:.2f}"
        if s["status"] == "resolved" and s["n"]:
            line += f"  wr={s['wins'] / s['n'] * 100:.1f}% ({s['wins']}/{s['n']})"
        print(line)
    print()

    # 3b. BY STRATEGY (resolved) — the per-regime fade canary scoreboard (LV/MV/HV).
    # avg_roi (pnl/size per trade) is sizing-neutral, so the three are comparable
    # even though they share one compounding bankroll.
    strat = cur.execute(
        """
        SELECT strategy,
               COUNT(*)                       AS n,
               COALESCE(SUM(pnl_usdc > 0), 0) AS wins,
               COALESCE(SUM(pnl_usdc), 0.0)   AS pnl,
               COALESCE(AVG(CASE WHEN size_usdc > 0 THEN pnl_usdc / size_usdc END), 0.0) AS avg_roi
          FROM positions
         WHERE status = 'resolved'
         GROUP BY strategy
         ORDER BY n DESC
        """
    ).fetchall()
    print("=== BY STRATEGY (resolved) ===")
    if not strat:
        print("  (none)")
    for s in strat:
        n = s["n"]
        wr = f"{s['wins'] / n * 100:.0f}%" if n else "-"
        print(
            f"  {_slabel(s['strategy']):<16} n={n:<4} wr={wr:<5} "
            f"avg_roi={s['avg_roi']:+.3f}  pnl={s['pnl']:+.2f}"
        )
    print()

    # 3c. BY EXIT — how positions LEFT: held to resolution vs closed early by SL / slow-rise.
    # All rows are strategy=contrarian (the ENTRY); this is what tells the exit overlays apart.
    byexit = cur.execute(
        """
        SELECT CASE WHEN status = 'resolved'             THEN 'hold (to resolve)'
                    WHEN closed_reason = 'time_gated_sl'  THEN 'SL (stop-loss)'
                    WHEN closed_reason = 'slow_rise_exit' THEN 'slow-rise exit'
                    ELSE COALESCE(closed_reason, 'closed') END AS via,
               COUNT(*)                       AS n,
               COALESCE(SUM(pnl_usdc > 0), 0) AS wins,
               COALESCE(SUM(pnl_usdc), 0.0)   AS pnl
          FROM positions
         WHERE status IN ('resolved', 'closed')
         GROUP BY via
         ORDER BY n DESC
        """
    ).fetchall()
    print("=== BY EXIT (all strategy=contrarian; how each position LEFT) ===")
    for e in byexit:
        n = e["n"]
        wr = f"{e['wins'] / n * 100:.0f}%" if n else "-"
        print(f"  {e['via']:<18} n={n:<4} wr={wr:<5} pnl={e['pnl']:+.2f}")
    print()

    # 4. Realized P&L + balance vs starting
    realized_row = cur.execute(
        "SELECT COALESCE(SUM(pnl_usdc), 0.0) AS pnl FROM positions WHERE status = 'resolved'"
    ).fetchone()
    realized = float(realized_row["pnl"])
    delta = balance - STARTING_BALANCE
    print("=== P&L ===")
    print(f"  realized P&L (resolved): {realized:+.2f}")
    print(f"  balance:                 {balance:.2f}  (start {STARTING_BALANCE:.2f})")
    print(f"  balance change:          {delta:+.2f}  ({delta / STARTING_BALANCE * 100:+.2f}%)")

    conn.close()


def main() -> None:
    _dump()


if __name__ == "__main__":
    main()
