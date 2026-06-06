"""
Standalone calibration-data fetcher (NOT part of the bot).

Pulls RESOLVED Up/Down crypto markets from Polymarket, reconstructs the "Up"
outcome price at fixed lead-times before resolution (T-60/30/15/5m), records the
final outcome, and stores everything in research/calibration.db.

Probe first:  python research/fetch_calibration_data.py --limit 100
Resumable: already-fetched condition_ids are skipped, so a killed run continues.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DB_PATH = Path(__file__).parent / "calibration.db"

# (column, minutes-before-resolution)
LEAD_TIMES = (("price_t60", 60), ("price_t30", 30), ("price_t15", 15), ("price_t5", 5))
# A reconstructed price must be within this many seconds of the target lead-time,
# else it's recorded NULL (sparse/missing history).
MATCH_TOLERANCE_S = 15 * 60

_UPDOWN_HINTS = ("up-or-down", "updown", "up or down")
_SUBHOURLY_HINTS = ("-5m-", "-15m-", "-30m-", "-1m-")
_SYMBOL_HINTS = {
    "BTC":  ("bitcoin", "btc"),
    "ETH":  ("ethereum", "eth"),
    "SOL":  ("solana", "sol"),
    "XRP":  ("xrp", "ripple"),
    "DOGE": ("dogecoin", "doge"),
    "BNB":  ("bnb", "binance coin"),
    "HYPE": ("hype", "hyperliquid"),
}


# ----------------------------- parsing helpers ------------------------------ #

def _blob(*texts: str) -> str:
    return " ".join(t or "" for t in texts).lower()


def _is_updown(*texts: str) -> bool:
    return any(h in _blob(*texts) for h in _UPDOWN_HINTS)


def _is_subhourly(*texts: str) -> bool:
    return any(h in _blob(*texts) for h in _SUBHOURLY_HINTS)


def _detect_symbol(*texts: str) -> str | None:
    blob = _blob(*texts)
    for sym, hints in _SYMBOL_HINTS.items():
        if any(h in blob for h in hints):
            return sym
    return None


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _token_ids(raw) -> list[str]:
    return [str(x) for x in _json_list(raw) if x]


def _parse_end(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_up(m: dict) -> tuple[str, int] | None:
    """Return (up_token_id, outcome) where outcome=1 if Up won, else 0.

    Outcomes/outcomePrices/clobTokenIds are parallel lists. For a resolved
    market outcomePrices is ["1","0"] or ["0","1"]. None if unresolvable.
    """
    outcomes = _json_list(m.get("outcomes"))
    prices = _json_list(m.get("outcomePrices"))
    tokens = _token_ids(m.get("clobTokenIds"))
    if len(tokens) < 2 or len(prices) < 2:
        return None
    up_idx = next((i for i, o in enumerate(outcomes) if str(o).strip().lower() == "up"), 0)
    up_token = tokens[up_idx] if up_idx < len(tokens) else tokens[0]
    try:
        outcome = 1 if float(prices[up_idx]) >= 0.5 else 0
    except (ValueError, IndexError):
        return None
    return up_token, outcome


def _price_at(history: list[dict], target_ts: int) -> float | None:
    if not history:
        return None
    best = min(history, key=lambda h: abs(int(h.get("t", 0)) - target_ts))
    if abs(int(best.get("t", 0)) - target_ts) > MATCH_TOLERANCE_S:
        return None
    try:
        return float(best.get("p"))
    except (TypeError, ValueError):
        return None


# ------------------------------- DB helpers --------------------------------- #

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration_data (
            condition_id   TEXT,
            symbol         TEXT,
            end_ts         TEXT,
            up_token_id    TEXT,
            outcome        INTEGER,
            price_t60      REAL,
            price_t30      REAL,
            price_t15      REAL,
            price_t5       REAL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cal_symbol ON calibration_data(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cal_end_ts ON calibration_data(end_ts)")
    conn.commit()


def existing_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT condition_id FROM calibration_data")}


# ------------------------------- HTTP layer --------------------------------- #

async def _get_json(session: aiohttp.ClientSession, url: str, params: dict, *, max_retries: int = 6):
    """GET with exponential backoff on 429 / transient errors. None on failure."""
    backoff = 1.0
    for _ in range(max_retries):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    return None


async def discover_resolved(
    session: aiohttp.ClientSession, limit: int, skip: set[str]
) -> list[dict]:
    """Page closed Up/Down events (newest first) until `limit` NEW markets gathered."""
    out: list[dict] = []
    per_page = 100
    offset = 0
    seen = set(skip)  # avoid in-run duplicates too
    while len(out) < limit:
        params = {
            "closed": "true",
            "limit": str(per_page),
            "offset": str(offset),
            "order": "endDate",
            "ascending": "false",
        }
        events = await _get_json(session, f"{GAMMA}/events", params)
        if not isinstance(events, list) or not events:
            break
        for event in events:
            if not isinstance(event, dict):
                continue
            ev_slug, ev_title = event.get("slug", ""), event.get("title", "")
            if not _is_updown(ev_slug, ev_title) or _is_subhourly(ev_slug, ev_title):
                continue
            for m in event.get("markets") or []:
                if not isinstance(m, dict):
                    continue
                cid = m.get("conditionId") or m.get("condition_id")
                if not cid or cid in seen:
                    continue
                symbol = _detect_symbol(m.get("question", ""), ev_slug, ev_title)
                end = _parse_end(m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso"))
                resolved = _resolve_up(m)
                if symbol is None or end is None or resolved is None:
                    continue
                up_token, outcome = resolved
                seen.add(cid)
                out.append({
                    "condition_id": cid,
                    "symbol": symbol,
                    "end": end,
                    "slug": ev_slug,
                    "up_token_id": up_token,
                    "outcome": outcome,
                })
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        offset += per_page
        if len(events) < per_page:
            break
    return out[:limit]


async def reconstruct_prices(session: aiohttp.ClientSession, up_token: str, end: datetime) -> dict:
    """One CLOB price-history call per market; derive all four lead-times from it."""
    end_unix = int(end.timestamp())
    start_unix = end_unix - (max(m for _, m in LEAD_TIMES) + 10) * 60
    data = await _get_json(
        session,
        f"{CLOB}/prices-history",
        {"market": up_token, "startTs": str(start_unix), "endTs": str(end_unix), "fidelity": "1"},
    )
    history = data.get("history", []) if isinstance(data, dict) else []
    prices = {}
    for col, mins in LEAD_TIMES:
        prices[col] = _price_at(history, end_unix - mins * 60)
    return prices


# --------------------------------- main ------------------------------------- #

async def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch resolved Up/Down markets for calibration.")
    ap.add_argument("--limit", type=int, default=100, help="max NEW markets to fetch (default 100, probe first)")
    ap.add_argument("--delay", type=float, default=0.15, help="seconds to sleep between markets (politeness)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    already = existing_ids(conn)
    print(f"db={DB_PATH}  already have {len(already)} markets  target +{args.limit} new")

    async with aiohttp.ClientSession() as session:
        print("discovering resolved Up/Down markets ...")
        markets = await discover_resolved(session, args.limit, already)
        print(f"discovered {len(markets)} new markets to fetch\n")

        total = len(markets)
        usable = 0
        for i, mk in enumerate(markets, 1):
            prices = await reconstruct_prices(session, mk["up_token_id"], mk["end"])
            conn.execute(
                """
                INSERT INTO calibration_data
                    (condition_id, symbol, end_ts, up_token_id, outcome,
                     price_t60, price_t30, price_t15, price_t5)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mk["condition_id"], mk["symbol"], mk["end"].isoformat(),
                    mk["up_token_id"], mk["outcome"],
                    prices["price_t60"], prices["price_t30"],
                    prices["price_t15"], prices["price_t5"],
                ),
            )
            conn.commit()  # commit per row so a kill mid-run loses nothing

            t30 = prices["price_t30"]
            if t30 is not None:
                usable += 1
            t30_s = f"{t30:.2f}" if t30 is not None else "NULL"
            outcome_s = "Up" if mk["outcome"] == 1 else "Down"
            print(f"fetched {i}/{total}: {mk['slug'][:40]}... outcome={outcome_s} t30={t30_s}")
            await asyncio.sleep(args.delay)

    _print_summary(conn, total, usable)
    conn.close()
    return 0


def _print_summary(conn: sqlite3.Connection, total: int, usable: int) -> None:
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    grand = conn.execute("SELECT COUNT(*) FROM calibration_data").fetchone()[0]
    print(f"markets fetched this run : {total}")
    print(f"  with usable t30 price  : {usable}/{total}")
    print(f"total rows in db         : {grand}")

    ups = conn.execute("SELECT COUNT(*) FROM calibration_data WHERE outcome=1").fetchone()[0]
    downs = conn.execute("SELECT COUNT(*) FROM calibration_data WHERE outcome=0").fetchone()[0]
    print(f"outcome distribution     : Up={ups}  Down={downs}")

    print("\nsample rows (condition_id, outcome, price_t30):")
    rows = conn.execute(
        "SELECT condition_id, outcome, price_t30 FROM calibration_data "
        "ORDER BY end_ts DESC LIMIT 5"
    ).fetchall()
    for cid, outcome, t30 in rows:
        t30_s = f"{t30:.3f}" if t30 is not None else "NULL"
        print(f"  {str(cid)[:24]}...  outcome={outcome}  t30={t30_s}")


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted — progress saved, re-run to resume.", file=sys.stderr)
        sys.exit(130)
