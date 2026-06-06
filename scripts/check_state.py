"""Read-only dump of the simulated trading state in data/bot.db.

Open positions show UNREALIZED pnl computed from the held token's CURRENT CLOB
price, so the script hits the network when there are open positions (it's no
longer fully offline). It still never writes — DB is opened mode=ro.
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

# scripts/ is not a package and src/ is not installed (pyproject: package = false),
# so put the project root on sys.path to reuse the bot's API client + config.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.polymarket import PolymarketREST, _normalize_outcome  # noqa: E402
from src.config import Settings  # noqa: E402
from src.execute.portfolio import SLIPPAGE_BUFFER  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"
STARTING_BALANCE = 1000.0


async def _fetch_unrealized(open_rows) -> dict[int, float | None]:
    """For each OPEN position, fetch the held token's current price from the same
    CLOB /markets/{condition_id} endpoint the resolver uses, and compute unrealized
    pnl matching the resolver's payout/cost convention (see portfolio.resolve_position).

    Returns {position_id: unrealized_pnl or None}. None => price unavailable, so the
    caller shows n/a for that row instead of crashing the whole dump.
    """
    s = Settings()
    out: dict[int, float | None] = {}

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
                    out[r["id"]] = None
                    return
                # eff_entry mirrors the slippage the resolver assumes on a winning
                # fill, so live_price->1 lands on the resolver's win pnl and ->0 on -size.
                entry = float(r["entry_price"])
                size = float(r["size_usdc"])
                eff = min(entry + SLIPPAGE_BUFFER, 1.0)
                out[r["id"]] = (size / eff) * price - size if eff else None
            except Exception:
                out[r["id"]] = None  # fail soft per-row; never crash the dump

        await asyncio.gather(*(one(r) for r in open_rows))
    return out


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
               status, exit_price, pnl_usdc, strategy
          FROM positions
         ORDER BY ts
        """
    ).fetchall()

    # Open rows get unrealized pnl from the live CLOB price (one network round-trip
    # each, run concurrently). market_id is used here for the lookup but not printed.
    open_rows = [p for p in positions if p["status"] == "open"]
    unreal = asyncio.run(_fetch_unrealized(open_rows)) if open_rows else {}
    print(f"=== POSITIONS ({len(positions)}) ===")
    if positions:
        print(
            f"  {'symbol':<8} {'side':<4} {'strategy':<12} {'entry':>7} {'size':>8} "
            f"{'status':<9} {'exit':>6} {'pnl':>10}"
        )
        for p in positions:
            exit_s = f"{p['exit_price']:.2f}" if p["exit_price"] is not None else "-"
            if p["status"] in ("resolved", "void"):
                pnl_s = f"{p['pnl_usdc']:.2f}" if p["pnl_usdc"] is not None else "-"
            else:
                # Open: unrealized estimate, marked with '~' to set it apart from
                # final/realized numbers; n/a when the live price couldn't be fetched.
                u = unreal.get(p["id"])
                pnl_s = f"~{u:+.2f}" if u is not None else "n/a"
            entry = p["entry_price"] if p["entry_price"] is not None else 0.0
            size = p["size_usdc"] if p["size_usdc"] is not None else 0.0
            print(
                f"  {str(p['symbol'] or '-'):<8} {str(p['side'] or '-'):<4} "
                f"{str(p['strategy'] or '-'):<12} "
                f"{entry:>7.3f} {size:>8.2f} {str(p['status']):<9} "
                f"{exit_s:>6} {pnl_s:>10}"
            )
    else:
        print("  (none)")
    print()

    # 3. Summary by status
    summary = cur.execute(
        """
        SELECT status,
               COUNT(*)                       AS n,
               COALESCE(SUM(pnl_usdc), 0.0)   AS total_pnl
          FROM positions
         GROUP BY status
         ORDER BY status
        """
    ).fetchall()
    print("=== BY STATUS ===")
    for s in summary:
        print(f"  {s['status']:<9} count={s['n']:<4} total_pnl={s['total_pnl']:.2f}")
    print()

    # 3b. Summary by strategy — per-strategy attribution (count + realized pnl).
    by_strategy = cur.execute(
        """
        SELECT strategy,
               COUNT(*)                       AS n,
               COALESCE(SUM(pnl_usdc), 0.0)   AS total_pnl
          FROM positions
         GROUP BY strategy
         ORDER BY strategy
        """
    ).fetchall()
    print("=== BY STRATEGY ===")
    for s in by_strategy:
        print(f"  {s['strategy']:<12} count={s['n']:<4} total_pnl={s['total_pnl']:.2f}")
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
