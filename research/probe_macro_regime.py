"""Retroactive macro vs regime (READ-ONLY, PAST data — macro is archived, no forward logger needed).

Fetch historical DAILY macro over the bot's regime window, split revert(<=06-15) vs efficient(>=06-16),
compare. NOTE: only n=1 transition exists -> this can only DESCRIBE (did macro differ between the two
periods) not ATTRIBUTE (is it a regime tell); the difference is confounded with calendar. Demonstrates
the underpowering concretely. Sources: Yahoo (VIX/DXY/US10Y), Binance (BTC/ETH klines + funding).
"""
import json
import math
import sqlite3
import statistics as st
import urllib.request
from datetime import date, datetime, timezone

UA = {"User-Agent": "Mozilla/5.0"}
SPLIT = date(2026, 6, 16)   # revert < SPLIT <= efficient (FINDINGS regime boundary)


def _get(url, timeout=25):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode())


def yahoo_series(sym, rng="3mo"):
    try:
        j = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={rng}")
        r = j["chart"]["result"][0]
        ts = r["timestamp"]
        cl = r["indicators"]["quote"][0]["close"]
        return {datetime.fromtimestamp(t, timezone.utc).date(): c for t, c in zip(ts, cl) if c is not None}
    except Exception as e:
        print(f"  yahoo {sym} ERR {e}")
        return {}


def binance_daily(symbol, limit=120):
    try:
        d = _get(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={limit}")
        return {datetime.fromtimestamp(k[0] / 1000, timezone.utc).date(): float(k[4]) for k in d}
    except Exception as e:
        print(f"  binance {symbol} ERR {e}")
        return {}


def funding_daily(symbol, limit=500):
    try:
        d = _get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
        by = {}
        for x in d:
            dt = datetime.fromtimestamp(x["fundingTime"] / 1000, timezone.utc).date()
            by.setdefault(dt, []).append(float(x["fundingRate"]))
        return {dt: st.mean(v) for dt, v in by.items()}
    except Exception as e:
        print(f"  funding {symbol} ERR {e}")
        return {}


def main():
    c = sqlite3.connect("file:data/bot.db?mode=ro", uri=True)
    rows = c.execute("SELECT MIN(resolve_time), MAX(resolve_time) FROM positions WHERE resolve_time IS NOT NULL").fetchone()
    lo = datetime.fromisoformat(rows[0].replace("Z", "+00:00")).date()
    hi = datetime.fromisoformat(rows[1].replace("Z", "+00:00")).date()
    print(f"bot regime window: {lo} -> {hi}  (split at {SPLIT})")

    vix, dxy, us10y = yahoo_series("^VIX"), yahoo_series("DX-Y.NYB"), yahoo_series("^TNX")
    btc, eth = binance_daily("BTCUSDT"), binance_daily("ETHUSDT")
    fund = funding_daily("BTCUSDT")
    bdates = sorted(d for d in btc if lo <= d <= hi)
    btc_ret = {bdates[i]: math.log(btc[bdates[i]] / btc[bdates[i - 1]]) for i in range(1, len(bdates))}

    def period(reg):
        days = [d for d in bdates if (d < SPLIT) == (reg == "revert")]
        rets = [btc_ret[d] for d in days if d in btc_ret]
        def m(series):
            xs = [series[d] for d in days if d in series]
            return st.mean(xs) if xs else None
        return {
            "n_days": len(days),
            "VIX": m(vix), "DXY": m(dxy), "US10Y": m(us10y),
            "BTC_RV(daily-ret stdev)": st.pstdev(rets) if len(rets) > 1 else None,
            "BTC_ret_mean": st.mean(rets) if rets else None,
            "funding": m(fund),
        }

    rv, ef = period("revert"), period("efficient")
    print(f"\n  {'metric':<26}{'REVERT':>12}{'EFFICIENT':>12}{'diff':>12}")
    for k in ("n_days", "VIX", "DXY", "US10Y", "BTC_RV(daily-ret stdev)", "BTC_ret_mean", "funding"):
        a, b = rv[k], ef[k]
        if a is None or b is None:
            print(f"  {k:<26}{str(a):>12}{str(b):>12}")
            continue
        d = b - a
        print(f"  {k:<26}{a:>12.4f}{b:>12.4f}{d:>+12.4f}")

    print("\nVERDICT: macro almost certainly DIFFERS between the two periods (everything moved over 2 weeks).")
    print("But this is ONE transition over ~2 weeks -> the difference = 'what macro prevailed in week-1 vs")
    print("week-2', perfectly confounded with calendar. CANNOT attribute to regime. n=1 -> 0 validatable DoF.")
    print("Insufficient (as predicted). Fix = MORE TRANSITIONS (keep BOT alive, wait) + re-fetch macro retro;")
    print("NOT a forward macro logger (macro is already fully fetchable).")


if __name__ == "__main__":
    main()
