"""
Retroactive backtest dari trade history di DB.

Usage: python -m script.backtest
"""
import io
import sys
import sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "bot_database.db"
SALDO_AWAL = 120.0
BASE_PCT, MIN_PCT, MAX_PCT = 0.15, 0.08, 0.20
MIN_BET = 5.0


# ── Helpers ────────────────────────────────────────────────────────────────────

_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]

def _extract_symbol(question: str) -> str:
    q = (question or "").upper()
    for s in _SYMBOLS:
        if s in q:
            return s
    return "?"


def _session(dt: datetime) -> str:
    h = dt.hour + dt.minute / 60.0
    if 13.5 <= h < 15.5:
        return "US_OPEN"
    if 15.5 <= h < 21.0:
        return "US_MAIN"
    if h >= 21.0 or h < 8.0:
        return "ASIA"
    return "EU"


def _price_bucket(p: float) -> str:
    for lo, hi in [(0.0, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 1.0)]:
        if lo <= p < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "other"


def _sim_bet(balance: float, last_5: list[float]) -> float:
    losses = wins = 0
    for pnl in reversed(last_5):
        if pnl > 0:
            if losses > 0:
                break
            wins += 1
        else:
            if wins > 0:
                break
            losses += 1
    if losses >= 2:
        pct = MIN_PCT
    elif wins >= 3:
        pct = MAX_PCT
    else:
        pct = BASE_PCT
    return max(MIN_BET, balance * pct)


# ── Load ───────────────────────────────────────────────────────────────────────

def load_trades() -> list[dict]:
    if not DB_PATH.exists():
        print(f"DB tidak ditemukan: {DB_PATH}")
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM positions
           WHERE status = 'closed' AND pnl_usdc IS NOT NULL
           ORDER BY exit_time ASC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Analysis helpers ───────────────────────────────────────────────────────────

def _wr_stats(items: list[float]) -> str:
    if not items:
        return "n=0"
    w = sum(1 for x in items if x > 0)
    return f"W={w} L={len(items)-w} WR={w/len(items)*100:.0f}%  avg=${sum(items)/len(items):+.2f}  total=${sum(items):+.2f}"


def _breakdown(trades: list[dict], key_fn, label: str):
    groups: dict[str, list[float]] = {}
    for t in trades:
        k = key_fn(t)
        groups.setdefault(k, []).append(float(t["pnl_usdc"]))
    print(f"\nBy {label}:")
    for k in sorted(groups):
        print(f"  {k:20s}: {_wr_stats(groups[k])}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    trades = load_trades()
    if not trades:
        print("Tidak ada closed trades di DB.")
        return

    print(f"\n{'='*80}")
    print(f"BACKTEST — {len(trades)} trades | SALDO_AWAL=${SALDO_AWAL:.2f}")
    print(f"{'='*80}")

    # ── Section 1: Equity Curve ─────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print(f"{'#':>4}  {'Time':19}  {'Sym':5}  {'Side':5}  "
          f"{'Ent':6}  {'Exit':6}  {'Bet_A':7}  {'Bet_S':7}  "
          f"{'PnL_A':8}  {'PnL_S':8}  {'Bal_A':8}  {'Bal_S':8}")
    print(f"{'─'*80}")

    bal_a = bal_s = SALDO_AWAL
    max_bal_a = max_bal_s = SALDO_AWAL
    min_bal_a = min_bal_s = SALDO_AWAL
    recent_actual: list[float] = []
    recent_sim: list[float] = []

    rows_enriched = []
    for i, t in enumerate(trades, 1):
        actual_bet = float(t["capital_at_risk"] or 0)
        actual_pnl = float(t["pnl_usdc"])
        sim_bet = _sim_bet(bal_s, recent_sim[-5:])
        sim_pnl = actual_pnl * (sim_bet / actual_bet) if actual_bet > 0 else actual_pnl

        bal_a += actual_pnl
        bal_s += sim_pnl

        max_bal_a = max(max_bal_a, bal_a)
        max_bal_s = max(max_bal_s, bal_s)
        min_bal_a = min(min_bal_a, bal_a)
        min_bal_s = min(min_bal_s, bal_s)

        sym = _extract_symbol(t["question"])
        ts = (t["exit_time"] or "")[:19]
        ent = float(t["entry_price"] or 0)
        ext = float(t["exit_price"] or 0)
        side = t["outcome"] or "?"

        print(f"{i:>4}  {ts:19}  {sym:5}  {side:5}  "
              f"{ent:6.3f}  {ext:6.3f}  "
              f"${actual_bet:6.2f}  ${sim_bet:6.2f}  "
              f"{actual_pnl:+8.2f}  {sim_pnl:+8.2f}  "
              f"${bal_a:7.2f}  ${bal_s:7.2f}")

        recent_actual.append(actual_pnl)
        recent_sim.append(sim_pnl)
        t["_sym"] = sym
        t["_session"] = _session(datetime.fromisoformat(
            (t["entry_time"] or "2000-01-01T00:00:00").replace("Z", "+00:00")
        ))
        rows_enriched.append(t)

    # ── Section 2: WR Breakdown ─────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print("BREAKDOWN")
    _breakdown(rows_enriched, lambda t: t["_sym"], "Symbol")
    _breakdown(rows_enriched, lambda t: (t["strategy_mode"] or "unknown"), "Strategy")
    _breakdown(rows_enriched, lambda t: (t["outcome"] or "?"), "Direction")
    _breakdown(rows_enriched, lambda t: t["_session"], "Session (entry)")
    _breakdown(rows_enriched, lambda t: _price_bucket(float(t["entry_price"] or 0)), "Entry Price Range")

    # ── Section 3: Summary ──────────────────────────────────────────────────
    actual_pnls = [float(t["pnl_usdc"]) for t in rows_enriched]
    # recompute sim equity for drawdown
    bal_s2 = SALDO_AWAL
    sim_pnls = []
    recent_s2: list[float] = []
    for t in rows_enriched:
        actual_bet = float(t["capital_at_risk"] or 0)
        actual_pnl = float(t["pnl_usdc"])
        sb = _sim_bet(bal_s2, recent_s2[-5:])
        sp = actual_pnl * (sb / actual_bet) if actual_bet > 0 else actual_pnl
        bal_s2 += sp
        sim_pnls.append(sp)
        recent_s2.append(sp)

    def max_dd(pnls):
        bal = SALDO_AWAL
        peak = bal
        dd = 0.0
        for p in pnls:
            bal += p
            peak = max(peak, bal)
            dd = max(dd, peak - bal)
        return dd

    wr_a = sum(1 for p in actual_pnls if p > 0) / len(actual_pnls) * 100
    wr_s = sum(1 for p in sim_pnls if p > 0) / len(sim_pnls) * 100

    print(f"\n{'─'*80}")
    print("SUMMARY")
    print(f"  {'':12}  {'Trades':>7}  {'WR':>6}  {'Total PnL':>10}  {'Final Bal':>10}  {'Max DD':>8}")
    print(f"  {'Actual':12}  {len(actual_pnls):>7}  {wr_a:>5.1f}%  "
          f"${sum(actual_pnls):>+9.2f}  ${SALDO_AWAL+sum(actual_pnls):>9.2f}  "
          f"${max_dd(actual_pnls):>7.2f}")
    print(f"  {'Simulated':12}  {len(sim_pnls):>7}  {wr_s:>5.1f}%  "
          f"${sum(sim_pnls):>+9.2f}  ${SALDO_AWAL+sum(sim_pnls):>9.2f}  "
          f"${max_dd(sim_pnls):>7.2f}")
    print(f"\n  Sim sizing: base={BASE_PCT:.0%} loss_streak={MIN_PCT:.0%} win_streak={MAX_PCT:.0%} floor=${MIN_BET}")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
