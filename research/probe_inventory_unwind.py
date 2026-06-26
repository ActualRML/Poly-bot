"""ONE cheap read-only test: INVENTORY UNWIND (no bot change, no new signal, single verdict).

Hypothesis: wallets that build a large one-sided TAKER position EARLY then UNWIND near expiry
(late flow opposes early), creating a small reversal. Tape = Data-API /trades (on-chain, per crypto
market); markets + resolve_time from bot.db. Windows: EARLY = before T-15min, LATE = last 15min.
Per (market,wallet,asset): early_net / late_net signed taker size (BUY=+, SELL=-).
PRIMARY (clean, tape-only): do big-early holders trade late at all, and do they REDUCE (unwind)?
  baseline: if late flow were random, ~50% of big holders would 'reduce' by chance.
SECONDARY (confounded by outcome-convergence, flagged): late price move vs early flow direction.
"""
import json
import sqlite3
import statistics as st
import time
import urllib.request
from collections import defaultdict
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0"}
N_MARKETS = 50


def pt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch(cid, max_n=1500):
    out, off = [], 0
    while len(out) < max_n:
        try:
            req = urllib.request.Request(f"https://data-api.polymarket.com/trades?market={cid}&limit=500&offset={off}", headers=UA)
            js = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
        except Exception:
            break
        if not js:
            break
        out += js
        off += len(js)
        if len(js) < 500:
            break
        time.sleep(0.2)
    return out


def main():
    c = sqlite3.connect("file:data/bot.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    mks = {}
    for r in c.execute("SELECT DISTINCT market_id, resolve_time FROM positions "
                       "WHERE exit_price IN (0,1,0.0,1.0) AND resolve_time IS NOT NULL "
                       "ORDER BY resolve_time DESC LIMIT ?", (N_MARKETS,)):
        rt = pt(r["resolve_time"])
        if rt:
            mks[r["market_id"]] = rt

    big_has_late = 0      # big-early holders that have ANY late trade
    big_total = 0
    reduce_among_late = 0
    reduce_frac = []      # fractional unwind among big-late
    mkt_early = []        # market aggregate early/late net flow (for corr)
    mkt_late = []
    price_rev = []        # secondary
    n_trades = 0

    for cid, rt in mks.items():
        tr = fetch(cid)
        if len(tr) < 30:
            continue
        n_trades += len(tr)
        early = defaultdict(float)   # (wallet,asset) -> net
        late = defaultdict(float)
        agg_e = agg_l = 0.0
        late_prices = []
        t15_price = None
        for t in sorted(tr, key=lambda x: x["timestamp"]):
            sgn = 1.0 if t["side"] == "BUY" else -1.0
            sz = float(t["size"]) * sgn
            key = (t["proxyWallet"], t["asset"])
            if t["timestamp"] < rt - 900:
                early[key] += sz
                agg_e += sz
                t15_price = float(t["price"])
            elif t["timestamp"] <= rt + 60:
                late[key] += sz
                agg_l += sz
                late_prices.append(float(t["price"]))
        if not early:
            continue
        mags = sorted(abs(v) for v in early.values())
        thr = mags[int(len(mags) * 0.75)] if len(mags) >= 4 else max(mags)
        for key, en in early.items():
            if abs(en) < thr or en == 0:
                continue
            big_total += 1
            ln = late.get(key, 0.0)
            if ln != 0.0:
                big_has_late += 1
                if ln * (-1 if en > 0 else 1) > 0:   # late opposes early = reduce/unwind
                    reduce_among_late += 1
                    reduce_frac.append(min(1.0, abs(ln) / abs(en)))
        if agg_e != 0:
            mkt_early.append(agg_e)
            mkt_late.append(agg_l)
        if t15_price is not None and late_prices and agg_e != 0:
            price_rev.append((late_prices[-1] - t15_price) * (-1 if agg_e > 0 else 1))

    print(f"markets used={len(mkt_early)}  trades pulled={n_trades}  big-early holders={big_total}")
    if big_total < 30:
        print("INSUFFICIENT DATA"); return
    print(f"\n[PRIMARY — unwind BEHAVIOR]")
    print(f"  big-early holders that trade LATE at all: {big_has_late}/{big_total} = {big_has_late/big_total*100:.1f}%")
    print(f"     (low % => they HOLD to settle = no unwind, expected for self-settling binaries)")
    if big_has_late:
        print(f"  of those, REDUCE (late opposes early): {reduce_among_late}/{big_has_late} = {reduce_among_late/big_has_late*100:.1f}%  (chance~50%)")
        if reduce_frac:
            print(f"     median fractional unwind: {st.median(reduce_frac)*100:.0f}% of the early position")
    # market-level corr(early agg flow, late agg flow)
    if len(mkt_early) >= 10:
        me, ml = st.mean(mkt_early), st.mean(mkt_late)
        num = sum((mkt_early[i]-me)*(mkt_late[i]-ml) for i in range(len(mkt_early)))
        den = (sum((x-me)**2 for x in mkt_early)*sum((x-ml)**2 for x in mkt_late))**0.5
        corr = num/den if den else 0
        print(f"  market-level corr(early flow, late flow) = {corr:+.3f}  (<0 = late opposes early = unwind)")
    print(f"\n[SECONDARY — price reversal, CONFOUNDED by outcome-convergence]")
    if price_rev:
        n=len(price_rev); m=st.mean(price_rev); se=st.pstdev(price_rev)/n**.5
        print(f"  late price move opposite early flow: {m:+.4f} +-{2*se:.4f} (n={n})  [>0=reversal, but ~outcome]")


if __name__ == "__main__":
    main()
