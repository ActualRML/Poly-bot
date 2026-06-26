"""READ-ONLY: is `persist` (persistent depth support) a REAL contrarian filter, or a COIN/LIQUIDITY
proxy? The decisive falsification of the 2026-06-21 depth-dynamics lead (persist AUC 0.74 efficient,
survived partial|price+dimb_now). Three tests (per the plan):

  1. WITHIN-COIN AUC — persist's win/loss separation INSIDE each coin (liquidity ~constant within a
     coin). If persist works inside BTC alone, it is NOT a cross-coin liquidity artifact.
  2. LOGISTIC REGRESSION  win ~ persist + price + depth_ratio(dimb_now) + coin [+ regime] — does the
     persist coefficient stay SIGNIFICANT controlling for everything at once? (IRLS from scratch; reports
     standardized coef + SE + z + p.)
  3. BTC/ETH-ONLY AUC — drop the thin coins; is persist still ~0.70 (all + efficient)?

`persist` = mean YES-side depth imbalance over the 12m pre-entry window, oriented to the HELD (fade)
side (built exactly as probe_depth_dynamics.py). Labels = contrarian ledger. data/bot_bt.db (indexed).
Run:  .venv/Scripts/python.exe research/probe_depth_persist_confound.py
"""
import math
import statistics
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = REPO / "data" / "bot_bt.db"
REVERT_LAST = "2026-06-15"
WINDOW_MIN = 12
EPS = 1e-6


def ep(s):
    return datetime.fromisoformat(s)


def auc(scores, labels):
    pairs = [(s, l) for s, l in zip(scores, labels) if s is not None]
    if not pairs:
        return None
    sc = [s for s, _ in pairs]; lb = [l for _, l in pairs]
    order = sorted(range(len(sc)), key=lambda i: sc[i])
    ranks = [0.0] * len(sc); i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and sc[order[j + 1]] == sc[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    P = sum(lb); N = len(lb) - P
    return None if P == 0 or N == 0 else (sum(ranks[i] for i in range(len(lb)) if lb[i]) - P * (P + 1) / 2.0) / (P * N)


def mat_inv(A):
    n = len(A)
    M = [list(A[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            M[col][col] += 1e-9
        d = M[col][col]
        M[col] = [v / d for v in M[col]]
        for r in range(n):
            if r != col:
                f = M[r][col]
                M[r] = [M[r][j] - f * M[col][j] for j in range(2 * n)]
    return [row[n:] for row in M]


def logreg(X, y, ridge=1e-3, iters=100):
    """IRLS logistic regression. Returns (beta, se). X includes intercept col."""
    n = len(X); k = len(X[0])
    beta = [0.0] * k
    XtWX = None
    for _ in range(iters):
        XtWX = [[0.0] * k for _ in range(k)]
        grad = [0.0] * k
        for i in range(n):
            eta = sum(X[i][j] * beta[j] for j in range(k))
            eta = max(-30.0, min(30.0, eta))
            p = 1.0 / (1.0 + math.exp(-eta))
            w = max(p * (1 - p), 1e-6)
            yi = y[i]
            for a in range(k):
                grad[a] += X[i][a] * (yi - p)
                xa = X[i][a]
                for b in range(k):
                    XtWX[a][b] += xa * w * X[i][b]
        for a in range(k):
            XtWX[a][a] += ridge
        inv = mat_inv(XtWX)
        delta = [sum(inv[a][b] * grad[b] for b in range(k)) for a in range(k)]
        for a in range(k):
            beta[a] += delta[a]
        if max(abs(d) for d in delta) < 1e-8:
            break
    inv = mat_inv(XtWX)
    se = [math.sqrt(max(inv[a][a], 0.0)) for a in range(k)]
    return beta, se


def zp(beta, se):
    z = beta / se if se > 0 else 0.0
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2))))
    return z, p


def znorm(vals):
    xs = [v for v in vals if v is not None]
    m = statistics.fmean(xs); s = statistics.pstdev(xs) or 1.0
    return [(v - m) / s if v is not None else 0.0 for v in vals]


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # YES-token assets (majority vote)
    tally = {}
    for r in conn.execute("SELECT asset_id, price, best_bid, best_ask FROM snapshots "
                          "WHERE source='polymarket' AND event_type='book' AND price IS NOT NULL AND asset_id IS NOT NULL"):
        ref = r["best_bid"] if r["best_bid"] is not None else r["best_ask"]
        if ref is None:
            continue
        yl = abs(r["price"] - ref) < EPS; nl = abs(r["price"] - (1.0 - ref)) < EPS
        if yl == nl:
            continue
        t = tally.setdefault(r["asset_id"], [0, 0]); t[0 if yl else 1] += 1
    yes = {a for a, (y, n) in tally.items() if y > n}

    pos = conn.execute("SELECT ts, market_id, symbol, side, entry_price, pnl_usdc FROM positions "
                       "WHERE strategy='contrarian' AND status='resolved' AND pnl_usdc IS NOT NULL ORDER BY ts").fetchall()
    T = []
    for p in pos:
        d = 1.0 if p["side"] == "YES" else -1.0
        lo = (ep(p["ts"]) - timedelta(minutes=WINDOW_MIN)).isoformat()
        rows = conn.execute("SELECT asset_id, bid_depth, ask_depth, ts FROM snapshots WHERE market_id=? "
                            "AND event_type='book' AND bid_depth IS NOT NULL AND ask_depth IS NOT NULL "
                            "AND ts<? AND ts>=? ORDER BY ts", (p["market_id"], p["ts"], lo)).fetchall()
        vals = []
        for r in rows:
            tot = r["bid_depth"] + r["ask_depth"]
            if tot <= 0:
                continue
            dimb = (r["bid_depth"] - r["ask_depth"]) / tot
            vals.append((dimb if r["asset_id"] in yes else -dimb) * d)
        if len(vals) < 2 or (ep(rows[-1]["ts"]) - ep(rows[0]["ts"])).total_seconds() < 240:
            continue
        T.append({"won": 1 if p["pnl_usdc"] > 0 else 0, "price": float(p["entry_price"]),
                  "coin": p["symbol"], "regime": "revert" if p["ts"][:10] <= REVERT_LAST else "efficient",
                  "persist": statistics.fmean(vals), "dimb_now": vals[-1]})
    conn.close()

    print("=" * 90)
    print(f"persist CONFOUND TESTS (coin/liquidity) — n={len(T)} computable contrarian trades")
    print("=" * 90)

    # ---- Test 1: within-coin AUC ----
    print("\n--- TEST 1: WITHIN-COIN AUC(persist, win) (liquidity ~ constant inside a coin) ---")
    coins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
    for c in coins:
        g = [t for t in T if t["coin"] == c]
        a = auc([t["persist"] for t in g], [t["won"] for t in g])
        W = sum(t["won"] for t in g)
        thin = "  <thin>" if len(g) < 20 or W < 5 or W == len(g) else ""
        print(f"    {c:<5} n={len(g):<3} W={W:<3} AUC={'n/a' if a is None else f'{a:.3f}'}{thin}")

    # ---- Test 2: logistic regression ----
    print("\n--- TEST 2: LOGISTIC  win ~ persist + price + dimb_now + coin [+ regime] (standardized) ---")
    persist_z = znorm([t["persist"] for t in T])
    price_z = znorm([t["price"] for t in T])
    dimb_z = znorm([t["dimb_now"] for t in T])
    coin_list = ["ETH", "SOL", "XRP", "DOGE", "BNB"]   # BTC = reference
    y = [t["won"] for t in T]
    for label, add_regime in (("coin", False), ("coin+regime", True)):
        X = []
        for i, t in enumerate(T):
            row = [1.0, persist_z[i], price_z[i], dimb_z[i]] + [1.0 if t["coin"] == c else 0.0 for c in coin_list]
            if add_regime:
                row.append(1.0 if t["regime"] == "efficient" else 0.0)
            X.append(row)
        beta, se = logreg(X, y)
        names = ["intercept", "persist", "price", "dimb_now"] + [f"coin={c}" for c in coin_list] + (["regime=eff"] if add_regime else [])
        zb, pb = zp(beta[1], se[1])
        sig = "  🔥 SIGNIFICANT" if pb < 0.05 and beta[1] > 0 else ("  (sig, NEG)" if pb < 0.05 else "  not sig")
        print(f"  model[{label}]: persist coef={beta[1]:+.3f} SE={se[1]:.3f} z={zb:+.2f} p={pb:.3f}{sig}")
        if not add_regime:
            for nm, b, s in zip(names, beta, se):
                if nm in ("price", "dimb_now"):
                    print(f"      ({nm}: coef={b:+.3f} p={zp(b, s)[1]:.3f})")

    # ---- Test 3: BTC/ETH only ----
    print("\n--- TEST 3: BTC/ETH ONLY (drop thin coins) — persist AUC ---")
    for rg in (None, "revert", "efficient"):
        g = [t for t in T if t["coin"] in ("BTC", "ETH") and (rg is None or t["regime"] == rg)]
        a = auc([t["persist"] for t in g], [t["won"] for t in g])
        W = sum(t["won"] for t in g)
        print(f"    {rg or 'ALL':<9} n={len(g):<3} W={W:<3} persist AUC={'n/a' if a is None else f'{a:.3f}'}")

    print("\nREAD: persist is REAL (not a coin/liquidity proxy) iff — within-coin BTC/ETH AUC stays high,")
    print("logistic persist coef stays + & p<0.05 controlling coin+price+depth, AND BTC/ETH-only AUC stays")
    print("~0.70. Any of these collapsing → it was liquidity. n thin (esp. efficient) → direction, not precision.")


if __name__ == "__main__":
    main()
