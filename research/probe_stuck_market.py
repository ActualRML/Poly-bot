"""
READ-ONLY probe: why does position id=5's market never resolve? (NOT part of the bot)

Queries the SAME CLOB endpoint the resolver uses -- GET {clob_url}/markets/{id} --
for the stuck market plus its two same-close siblings, prints the full raw
response, and classifies which of get_market_resolution's four `return None`
paths each market hits:

    (1) HTTP/transport error (404/timeout/...)      -> resolver returns None
    (2) closed is falsy (not finalized on CLOB)     -> resolver returns None
    (3) closed=True but NO token has winner==True    -> resolver returns None  (void/refund)
    (4) winner outcome doesn't map to YES/NO         -> resolver returns None

READ-ONLY guarantees: imports PolymarketREST + Settings from src/ (no edits, no
bot changes); opens data/bot.db with mode=ro; runs in its own process with its
own logger/session. It does NOT call require_credentials() and never writes.

Run manually (do not run during anything that needs the network blocked):

    python research/probe_stuck_market.py
"""

import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

import aiohttp

# --- make `import src.*` resolve regardless of the current working directory ---
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.api.polymarket import PolymarketREST, _normalize_outcome  # noqa: E402
from src.config import Settings  # noqa: E402

# Position id=5's market: ETH YES, closed 03:00 UTC, still open 11.5h+ later
# while siblings id=6/id=7 (same 03:00 close) resolved ~10h ago.
TARGET_MARKET_ID = "0x40894f45a7ec50b4b08af9138a3f72fb285cb34c162b10db083e7a7226e3d850"
COHORT_IDS = (5, 6, 7)               # 5 = stuck; 6/7 = resolved siblings (comparison)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


# --------------------------------- DB read --------------------------------- #

def load_cohort(db_path: Path) -> list[dict]:
    """Read the id 5/6/7 rows from positions, strictly READ-ONLY (uri mode=ro)."""
    uri = db_path.as_uri() + "?mode=ro"          # file:///.../bot.db?mode=ro
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, symbol, side, status, market_id, resolve_time, resolved_ts "
            "FROM positions WHERE id IN (?,?,?) ORDER BY id",
            COHORT_IDS,
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# ------------------------------- CLOB fetch -------------------------------- #

async def raw_clob_market(rest: PolymarketREST, market_id: str):
    """
    Hit the exact endpoint the resolver uses, but WITHOUT raise_for_status, so we
    can see the status code + body even on a 404 (the resolver's _get would raise
    here and the caller would just get None -- we want the actual bytes).

    Reuses the resolver's own aiohttp session for maximum fidelity.
    Returns (status, body, error):
      - status: int HTTP status, or None on a transport failure
      - body:   parsed JSON (dict/list) if parseable, else raw text, else None
      - error:  str if a transport-level exception fired (== resolver path 1), else None
    """
    assert rest._session is not None, "session not open -- use 'async with PolymarketREST(...)'"
    url = f"{rest.clob_url}/markets/{market_id}"
    try:
        async with rest._session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
            text = await resp.text()
    except Exception as e:                        # timeout / DNS / connection reset
        return None, None, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(text), None
    except Exception:
        return status, text, None                 # non-JSON (HTML error page, etc.)


# ----------------------------- classification ------------------------------ #

def classify(status, body, error) -> tuple[str, dict | None]:
    """
    Replicate get_market_resolution's branching to identify WHICH None-path fires.
    Returns (verdict, winner_token_or_None).
    """
    # path 1 -- transport error, OR any >=400 (resolver's _get does raise_for_status)
    if error is not None:
        return f"HTTP/transport error: {error}  -> path (1): resolver returns None", None
    if status is not None and status >= 400:
        return f"HTTP {status}  -> market delisted/stale id  -> path (1): resolver returns None", None

    # path 2 -- not a dict, or 'closed' falsy
    if not isinstance(body, dict):
        return f"response is {type(body).__name__}, not a dict  -> path (2): resolver returns None", None
    closed = body.get("closed")
    if not closed:
        return f"closed={closed!r}  -> market NOT finalized on CLOB  -> path (2): resolver returns None", None

    # path 3 -- closed but no winner token flagged
    winner = next(
        (t for t in body.get("tokens") or [] if isinstance(t, dict) and t.get("winner")),
        None,
    )
    if winner is None:
        return "closed=True but NO winner token  -> VOID/refund/not-flagged  -> path (3): resolver returns None", None

    # path 4 -- winner outcome doesn't normalize to YES/NO
    outcome = _normalize_outcome(winner.get("outcome", ""))
    if outcome is None:
        return (f"closed=True, winner outcome={winner.get('outcome')!r} does NOT map to YES/NO "
                f"-> path (4): resolver returns None"), winner

    # success -- the resolver would settle this
    return (f"resolved cleanly: winner outcome={winner.get('outcome')!r} -> '{outcome}'  "
            f"-> SHOULD have resolved (UNEXPECTED -- investigate resolver, not the market)"), winner


# -------------------------------- printing --------------------------------- #

def _indent(s: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in s.splitlines())


def print_tokens(body) -> None:
    if not isinstance(body, dict):
        print("    tokens: (n/a -- response is not a dict)")
        return
    tokens = body.get("tokens")
    if not tokens:
        print(f"    tokens: {tokens!r}  (empty/missing)")
        return
    print(f"    tokens ({len(tokens)}):")
    for i, t in enumerate(tokens):
        if not isinstance(t, dict):
            print(f"      [{i}] (non-dict: {t!r})")
            continue
        outcome = repr(t.get("outcome"))
        price = str(t.get("price"))
        win = str(t.get("winner"))
        tid = str(t.get("token_id"))
        print(f"      [{i}] outcome={outcome:<8} price={price:<12} "
              f"winner={win:<6} token_id={tid[:22]}{'...' if len(tid) > 22 else ''}")


async def probe_one(rest: PolymarketREST, label: str, market_id: str) -> None:
    print("=" * 80)
    print(label)
    print(f"  market_id: {market_id}")
    print(f"  endpoint : GET {rest.clob_url}/markets/{market_id}")
    print("-" * 80)

    status, body, error = await raw_clob_market(rest, market_id)

    # (2) full raw response
    print(f"  HTTP status: {status if status is not None else '(transport error)'}")
    if error:
        print(f"  TRANSPORT ERROR: {error}")
    print("  RAW response:")
    if isinstance(body, (dict, list)):
        print(_indent(json.dumps(body, indent=2, sort_keys=True), 4))
    elif body is not None:
        print(_indent(str(body)[:4000], 4))

    # (3) mirror get_market_resolution's exact checks
    print("\n  --- get_market_resolution() mirror ---")
    if isinstance(body, dict):
        print("    isinstance dict : True")
        print(f"    closed          : {body.get('closed')!r}")
    else:
        print(f"    isinstance dict : False ({type(body).__name__})")
    print_tokens(body)

    verdict, winner = classify(status, body, error)
    if winner is not None:
        print(f"    winner token    : outcome={winner.get('outcome')!r} price={winner.get('price')!r}")
        print(f"    _normalize_outcome -> {_normalize_outcome(winner.get('outcome', ''))!r}")

    # authoritative cross-check: what the REAL resolver function returns for this id
    try:
        actual = await rest.get_market_resolution(market_id)
    except Exception as e:
        actual = f"<raised {type(e).__name__}: {e}>"
    print(f"\n    get_market_resolution() actually returns: {actual!r}")

    # (4) verdict
    print(f"\n  VERDICT: {verdict}")
    print()


# ---------------------------------- main ----------------------------------- #

async def main() -> None:
    # Surface the resolver's own WARNING line (it logs on path-1 errors) so we see
    # exactly what the bot would have logged. Read-only side effect: stderr only.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = Settings()
    db_path = settings.db_path
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    db_path = db_path.resolve()

    print("READ-ONLY PROBE -- Polymarket CLOB resolution diagnostic")
    print(f"  clob_url: {settings.polymarket_clob_url}")
    print(f"  db_path : {db_path}  (opened mode=ro)\n")

    # cohort straight from the DB (read-only) -- single source of truth for siblings
    cohort: list[dict] = []
    try:
        cohort = load_cohort(db_path)
    except Exception as e:
        print(f"WARN: could not read cohort from DB: {type(e).__name__}: {e}\n")

    print("COHORT (positions id 5/6/7 -- the 03:00 close):")
    if cohort:
        for r in cohort:
            print(f"  id={r['id']} {r['symbol']:<4} {r['side']:<3} status={r['status']:<8} "
                  f"resolved_ts={r['resolved_ts']}  market_id={r['market_id']}")
    else:
        print("  (none found)")
    print()

    # build probe list: TARGET (id=5) first, then each resolved sibling from the DB
    id5 = next((r for r in cohort if r["id"] == 5), None)
    if id5 and id5["market_id"] != TARGET_MARKET_ID:
        print(f"NOTE: id=5 DB market_id ({id5['market_id']}) != TARGET constant "
              f"({TARGET_MARKET_ID}); probing the TARGET constant.\n")

    probe_list: list[tuple[str, str]] = [
        ("TARGET -- position id=5 (STUCK, ETH YES, closed 03:00 UTC)", TARGET_MARKET_ID)
    ]
    for r in cohort:
        if r["id"] == 5:
            continue
        probe_list.append(
            (f"SIBLING -- position id={r['id']} ({r['symbol']} {r['side']}, status={r['status']})",
             r["market_id"])
        )

    async with PolymarketREST(settings.polymarket_gamma_url, settings.polymarket_clob_url) as rest:
        for label, mid in probe_list:
            await probe_one(rest, label, mid)

    print("=" * 80)
    print("Done. READ-ONLY: no bot state touched, no src/ edits, data/bot.db opened mode=ro.")
    print("If the TARGET hits path (3) (closed, no winner) while siblings resolved cleanly,")
    print("the market voided/refunded and the resolver has no path to settle it.")


if __name__ == "__main__":
    asyncio.run(main())
