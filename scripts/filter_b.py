"""PRE-REGISTERED Filter Test — ARM B (holistik / diskresioner), FORWARD-ONLY.

Locked 2026-06-25 (see the pre-registration). Arm B is a human take/skip overlay on the
UNCHANGED contrarian entries. It CANNOT be backtested (a discretionary call read off
history is hindsight) — so every call is LOGGED with a timestamp BEFORE the market
resolves, and joined to the outcome only afterwards. Pure logging: this CLI never touches
the bot's entry engine or its DB (opens data/bot.db read-only, appends to its OWN log).

Candidate universe = the markets contrarian actually ENTERED (Baseline takes them all; a
filter may only SKIP). So `list` shows OPEN contrarian positions still pre-resolve; you
record take/skip (+ optional side/size/max_price override) while the market is LIVE.

LOCKED CRITERIA (shared with arm A): pass iff net-WR margin >= break-even + 8pp AND N>=100.
Below 100 => NO CONCLUSION. Evaluate ONCE at the locked sample; no peek-and-extend.

Usage:
  python scripts/filter_b.py list
  python scripts/filter_b.py call --market <id> --take --side NO --size 5 --max-price 0.30 [--note "..."]
  python scripts/filter_b.py call --market <id> --skip --note "thin book"
  python scripts/filter_b.py score
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "bot.db"
LOG = REPO / "data" / "filter_b_calls.jsonl"
BUFFER_PP = 0.08
MIN_N = 100
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _now():
    return datetime.now(timezone.utc)


def _called_ids():
    if not LOG.exists():
        return {}
    out = {}
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["market_id"]] = r
    return out


def _latest_ctx(conn, market_id):
    """Most recent zone/vol the bot tagged for this market (for the human's read)."""
    r = conn.execute(
        "SELECT price_zone, vol_regime FROM snapshots WHERE market_id=? AND price_zone IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (market_id,)).fetchone()
    return (r["price_zone"], r["vol_regime"]) if r else ("-", "-")


def _mins_left(rt):
    if not rt:
        return None
    try:
        return (datetime.fromisoformat(rt) - _now()).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def cmd_list(_args):
    conn = _conn()
    called = _called_ids()
    rows = conn.execute(
        "SELECT market_id, symbol, side, entry_price, resolve_time FROM positions "
        "WHERE strategy='contrarian' AND status='open' ORDER BY ts").fetchall()
    pending = [r for r in rows if r["market_id"] not in called]
    print(f"=== CONTRARIAN CANDIDATES (open, pre-resolve, not yet called) — {len(pending)} ===")
    if not pending:
        print("  (none — all open positions already have a call, or none are open)")
        conn.close()
        return
    print(f"  {'symbol':<7} {'c.side':<6} {'entry':>6} {'zone':<12} {'vol':<9} {'resolves':>9}  market_id")
    for r in pending:
        zone, vol = _latest_ctx(conn, r["market_id"])
        ml = _mins_left(r["resolve_time"])
        mls = "due" if ml is not None and ml <= 0 else (f"{ml:.0f}m" if ml is not None else "-")
        print(f"  {str(r['symbol'] or '-'):<7} {str(r['side'] or '-'):<6} {float(r['entry_price']):>6.3f} "
              f"{str(zone):<12} {str(vol):<9} {mls:>9}  {r['market_id']}")
    print("\nRecord a call BEFORE it resolves:")
    print("  python scripts/filter_b.py call --market <id> --take --side <YES/NO> --size <n> --max-price <p>")
    print("  python scripts/filter_b.py call --market <id> --skip --note \"...\"")
    conn.close()


def cmd_call(args):
    if bool(args.take) == bool(args.skip):
        sys.exit("specify exactly one of --take / --skip")
    conn = _conn()
    pos = conn.execute(
        "SELECT market_id, symbol, side, entry_price, resolve_time, status FROM positions "
        "WHERE strategy='contrarian' AND market_id=? ORDER BY ts DESC LIMIT 1", (args.market,)).fetchone()
    if pos is None:
        sys.exit(f"no contrarian position for market {args.market} (only contrarian entries are callable)")
    ml = _mins_left(pos["resolve_time"])
    if ml is not None and ml <= 0:
        sys.exit("market already at/after resolve_time -- FORWARD-ONLY rule: cannot log a call post-resolve")
    if args.market in _called_ids():
        sys.exit("already called this market (no double-call — one decision per candidate, locked)")
    rec = {
        "call_ts": _now().isoformat(),
        "market_id": args.market,
        "symbol": pos["symbol"],
        "decision": "take" if args.take else "skip",
        "side": args.side if args.take else None,
        "size_usdc": args.size if args.take else None,
        "max_price": args.max_price if args.take else None,
        "note": args.note or "",
        "contrarian_side": pos["side"],
        "contrarian_entry": float(pos["entry_price"]),
        "resolve_time": pos["resolve_time"],
    }
    if args.take and (rec["side"] is None or rec["max_price"] is None):
        sys.exit("--take needs --side and --max-price (the effective limit you'd pay)")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    conn.close()
    print(f"logged {rec['decision'].upper()} {rec['symbol']} {args.market} @ {rec['call_ts']}")


def _outcome(side, pnl):
    """Market truth from the contrarian position: it won iff its side == outcome."""
    if pnl is None:
        return None
    if pnl > 0:
        return side
    return "NO" if side == "YES" else "YES"


def _arm_stats(items):
    """items = [(won_bool, price)] ; price = effective break-even cost."""
    n = len(items)
    if n == 0:
        return None
    wins = sum(1 for w, _ in items if w)
    wr = wins / n
    be = sum(p for _, p in items) / n
    ev_roi = sum(((1 - p) / p if w else -1.0) for w, p in items) / n
    return {"n": n, "wins": wins, "wr": wr, "be": be, "margin": wr - be, "ev_roi": ev_roi}


def _show(label, a):
    if a is None:
        print(f"  {label:<22} (0)")
        return
    print(f"  {label:<22} N={a['n']:<4} rawWR={a['wr']*100:4.1f}%  break-even={a['be']*100:4.1f}%  "
          f"margin={a['margin']*100:+5.1f}pp  EV/trade(roi)={a['ev_roi']:+.3f}")


def cmd_score(_args):
    conn = _conn()
    calls = list(_called_ids().values())
    base_items, take_items = [], []
    pending = 0
    for c in calls:
        pos = conn.execute(
            "SELECT side, pnl_usdc, status FROM positions WHERE strategy='contrarian' AND market_id=? "
            "ORDER BY ts DESC LIMIT 1", (c["market_id"],)).fetchone()
        if pos is None or pos["status"] not in ("resolved", "closed") or pos["pnl_usdc"] is None:
            pending += 1
            continue
        outcome = _outcome(pos["side"], pos["pnl_usdc"])
        # Baseline (control): contrarian's own side/entry on EVERY market the human reviewed.
        base_items.append((pos["side"] == outcome, float(c["contrarian_entry"])))
        # Filter B: only the TAKE calls, scored on the human's chosen side/limit.
        if c["decision"] == "take":
            take_items.append((c["side"] == outcome, float(c["max_price"])))
    conn.close()

    print("=" * 92)
    print("PRE-REGISTERED FILTER TEST — ARM B (holistik), FORWARD-ONLY")
    print(f"  calls logged: {len(calls)}   resolved: {len(calls) - pending}   pending: {pending}")
    print(f"  pass: margin >= break-even + {BUFFER_PP*100:.0f}pp  AND  N>={MIN_N}")
    print("=" * 92)
    base, take = _arm_stats(base_items), _arm_stats(take_items)
    _show("Baseline (all reviewed)", base)
    _show("Filter B (take-subset)", take)
    if base and take:
        print(f"\n  Δmargin (take - baseline) = {(take['margin'] - base['margin'])*100:+.1f}pp")
    print()
    if take is None or take["n"] < MIN_N:
        have = take["n"] if take else 0
        print(f"  VERDICT: N={have} < {MIN_N} -> NO CONCLUSION (forward-accruing; {MIN_N - have} more take-calls)")
    else:
        print(f"  VERDICT: margin={take['margin']*100:+.1f}pp vs +{BUFFER_PP*100:.0f}pp -> "
              f"{'PASS' if take['margin'] >= BUFFER_PP else 'FAIL'}")


def main():
    ap = argparse.ArgumentParser(description="Filter Test arm B — discretionary, forward-only.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show open contrarian candidates not yet called")
    cp = sub.add_parser("call", help="log a take/skip decision (timestamped, pre-resolve)")
    cp.add_argument("--market", required=True)
    cp.add_argument("--take", action="store_true")
    cp.add_argument("--skip", action="store_true")
    cp.add_argument("--side", choices=["YES", "NO"])
    cp.add_argument("--size", type=float)
    cp.add_argument("--max-price", type=float, dest="max_price")
    cp.add_argument("--note", default="")
    sub.add_parser("score", help="join calls to resolutions, per-arm scoreboard")
    args = ap.parse_args()
    {"list": cmd_list, "call": cmd_call, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
