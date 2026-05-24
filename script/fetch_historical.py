"""
script/fetch_historical.py — Polymarket UP/DOWN Hourly Historical Data Fetcher
===============================================================================
Standalone fetcher. Zero dependency on live bot logic (src/* untouched).
Output goes to data/historical.db — never touches bot_database.db.

PURPOSE
-------
Fetch raw historical trade data from resolved Polymarket UP/DOWN hourly markets
for the 6 crypto symbols (BTC/ETH/SOL/XRP/DOGE/BNB). Stores market metadata
and per-fill trade records for downstream backtest analysis.

DATA SOURCES
------------
  Market discovery : GET https://gamma-api.polymarket.com/events
                     (slug prefix match for crypto UP/DOWN hourly; closed=true)
  Trade history    : GET https://data-api.polymarket.com/trades?market=COND_ID&limit=500
                     (paginated with offset, works for resolved markets)

SCHEMA (data/historical.db)
---------------------------
  markets  — one row per resolved UP/DOWN hourly market
    condition_id  TEXT PK
    slug          TEXT
    symbol        TEXT  (BTC/ETH/SOL/XRP/DOGE/BNB)
    question      TEXT
    start_ts      INTEGER (unix s)
    end_ts        INTEGER (unix s)
    resolved_outcome TEXT  ("Up" / "Down" / NULL if unknown)
    token_id_up   TEXT
    token_id_down TEXT
    fetched_at    INTEGER (unix s, when metadata was written)

  trades — raw fills from data-api, linked by condition_id
    id            INTEGER PK AUTOINCREMENT
    condition_id  TEXT  FK→markets
    token_id      TEXT  (per-outcome asset)
    timestamp     INTEGER (unix s)
    price         REAL
    size          REAL  (USDC)
    side          TEXT  (BUY/SELL)
    outcome       TEXT  (Up/Down — mapped from token_id or outcome field)
    fetched_at    INTEGER

USAGE
-----
  # Small test: last 7 days, metadata only
  python -m script.fetch_historical --days 7 --markets-only

  # Full fetch: last 30 days, all symbols
  python -m script.fetch_historical --days 30

  # Specific symbols, verbose
  python -m script.fetch_historical --days 14 --symbols BTC,ETH --verbose

  # Dry run (no DB writes, prints what would be fetched)
  python -m script.fetch_historical --days 7 --dry-run

  # Skip markets already in DB
  python -m script.fetch_historical --days 30 --skip-existing

SAMPLE QUERIES
--------------
  -- Trade count per symbol
  SELECT m.symbol, COUNT(t.id) trades, COUNT(DISTINCT t.condition_id) markets
  FROM trades t JOIN markets m USING(condition_id)
  GROUP BY m.symbol ORDER BY trades DESC;

  -- Price time-series for one market
  SELECT datetime(timestamp, 'unixepoch') ts, price, side, outcome
  FROM trades WHERE condition_id = '0xABCD...'
  ORDER BY timestamp;

  -- Win rate per symbol (which direction resolved)
  SELECT symbol, resolved_outcome, COUNT(*) markets
  FROM markets WHERE resolved_outcome IS NOT NULL
  GROUP BY symbol, resolved_outcome ORDER BY symbol;

  -- Average price at entry (first 10 min) vs resolution
  SELECT m.symbol, m.resolved_outcome,
         AVG(CASE WHEN t.timestamp < m.start_ts + 600 THEN t.price END) entry_avg,
         AVG(CASE WHEN t.timestamp > m.end_ts   - 300 THEN t.price END) final_avg,
         COUNT(*) total_trades
  FROM trades t JOIN markets m USING(condition_id)
  GROUP BY m.symbol, m.resolved_outcome;

DISCLAIMER
----------
Data fetcher only. Backtest engine is a separate task.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import aiohttp

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = ROOT_DIR / "data" / "historical.db"

GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"

SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]

# Slug prefixes used by scanner.py — primary symbol detection for /events
_UPDOWN_SLUG_PREFIXES: dict[str, str] = {
    "BTC":  "bitcoin-up-or-down-",
    "ETH":  "ethereum-up-or-down-",
    "SOL":  "solana-up-or-down-",
    "XRP":  "xrp-up-or-down-",
    "DOGE": "dogecoin-up-or-down-",
    "BNB":  "bnb-up-or-down-",
}

# Skip markers for 5m/15m/4h variants (not hourly)
_UPDOWN_SKIP_MARKERS = (
    "-5m-", "-15m-", "-4h-",
    "updown-5m", "updown-15m", "updown-4h",
    "updown-1m", "updown-30m",
)

# Fallback keyword maps for symbol detection when slug prefix doesn't match
_SYMBOL_KEYWORDS: list[tuple[str, str]] = [
    ("BITCOIN",  "BTC"),
    ("ETHEREUM", "ETH"),
    ("SOLANA",   "SOL"),
    ("DOGECOIN", "DOGE"),
    ("DOGE",     "DOGE"),
    ("RIPPLE",   "XRP"),
    ("XRP",      "XRP"),
    ("BINANCE",  "BNB"),
    ("BNB",      "BNB"),
    ("ETH",      "ETH"),
    ("SOL",      "SOL"),
    ("BTC",      "BTC"),
]

# slug / question must contain one of these to be an UP/DOWN market (fallback check)
_UPDOWN_MARKERS = [
    "up-or-down", "up or down", "higher-or-lower", "higher or lower",
]

CONCURRENT_LIMIT = 6          # max concurrent trade-fetch coroutines
REQ_DELAY = 0.15              # seconds between API requests
MAX_RETRIES = 3
GAMMA_PAGE_LIMIT = 100        # results per gamma /events call
TRADE_PAGE_LIMIT = 500        # results per data-api /trades call

log = logging.getLogger("fetch_historical")


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS markets (
    condition_id     TEXT PRIMARY KEY,
    slug             TEXT,
    symbol           TEXT,
    question         TEXT,
    start_ts         INTEGER,
    end_ts           INTEGER,
    resolved_outcome TEXT,
    token_id_up      TEXT,
    token_id_down    TEXT,
    fetched_at       INTEGER
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    token_id     TEXT,
    timestamp    INTEGER,
    price        REAL,
    size         REAL,
    side         TEXT,
    outcome      TEXT,
    fetched_at   INTEGER,
    FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique
    ON trades(condition_id, timestamp, price, size, side, outcome);

CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_markets_end_ts   ON markets(end_ts);
CREATE INDEX IF NOT EXISTS idx_markets_symbol   ON markets(symbol);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(DDL)
    conn.commit()
    return conn


def existing_condition_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT condition_id FROM markets").fetchall()
    return {r[0] for r in rows}


def existing_trade_cids(conn: sqlite3.Connection) -> set[str]:
    """condition_ids that already have at least one trade row."""
    rows = conn.execute(
        "SELECT DISTINCT condition_id FROM trades"
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slug_to_symbol(slug: str) -> Optional[str]:
    """Map an event slug to a symbol via prefix matching (same as scanner.py)."""
    s = (slug or "").lower()
    for sym, prefix in _UPDOWN_SLUG_PREFIXES.items():
        if s.startswith(prefix):
            return sym
    return None


def has_skip_marker(slug: str) -> bool:
    s = (slug or "").lower()
    return any(marker in s for marker in _UPDOWN_SKIP_MARKERS)


def detect_symbol(text: str) -> Optional[str]:
    t = (text or "").upper().replace("-", " ")
    for keyword, sym in _SYMBOL_KEYWORDS:
        if keyword in t:
            return sym
    return None


def is_updown_market(slug: str, question: str) -> bool:
    combined = ((slug or "") + " " + (question or "")).lower()
    return any(m in combined for m in _UPDOWN_MARKERS)


def parse_ts(val) -> Optional[int]:
    """Parse a datetime string or unix int/float to unix seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = int(val)
        # nanoseconds → seconds if too large
        if v > 1e12:
            v = v // 1000
        if v > 1e10:
            v = v // 1000
        return v
    s = str(val).strip()
    if not s:
        return None
    # try numeric
    try:
        return parse_ts(float(s))
    except ValueError:
        pass
    # try ISO
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception:
        return None


def resolve_outcome(outcomes: list[str], prices: list[str]) -> Optional[str]:
    """Return the winning outcome name based on outcomePrices."""
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    try:
        idx = max(range(len(prices)), key=lambda i: float(prices[i] or 0))
        return outcomes[idx]
    except Exception:
        return None


def extract_tokens(
    outcomes: list[str],
    token_ids: list[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return (token_id_up, token_id_down)."""
    up_id = down_id = None
    for i, outcome in enumerate(outcomes):
        if i >= len(token_ids):
            break
        tid = token_ids[i]
        if outcome.strip().lower() == "up":
            up_id = tid
        elif outcome.strip().lower() == "down":
            down_id = tid
    return up_id, down_id


def outcome_for_token(
    token_id: str,
    token_id_up: Optional[str],
    token_id_down: Optional[str],
) -> Optional[str]:
    if token_id and token_id == token_id_up:
        return "Up"
    if token_id and token_id == token_id_down:
        return "Down"
    return None


def _parse_json_field(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

async def _get(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
    retries: int = MAX_RETRIES,
) -> Optional[list | dict]:
    for attempt in range(retries + 1):
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 429:
                    wait = 2 ** attempt + 2
                    log.warning(f"Rate-limited (429) — waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if resp.status in (500, 502, 503, 504):
                    wait = 2 ** attempt
                    log.warning(f"HTTP {resp.status} for {url} — retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    log.error(f"HTTP {resp.status} for {url}")
                    return None
                return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            wait = 2 ** attempt
            log.warning(f"Timeout {url} (attempt {attempt+1}) — retry in {wait}s")
            await asyncio.sleep(wait)
        except Exception as e:
            log.error(f"Request error {url}: {e}")
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
            else:
                return None
    log.error(f"Exhausted retries for {url}")
    return None


# ---------------------------------------------------------------------------
# Market discovery (Gamma /events API)
# ---------------------------------------------------------------------------

async def fetch_events_page(
    session: aiohttp.ClientSession,
    offset: int,
    limit: int = GAMMA_PAGE_LIMIT,
    end_date_min: Optional[str] = None,
) -> list[dict]:
    """Fetch one page of closed events from gamma /events."""
    params: dict = {
        "closed":    "true",
        "limit":     limit,
        "offset":    offset,
        "order":     "endDate",
        "ascending": "false",
    }
    if end_date_min:
        params["end_date_min"] = end_date_min

    data = await _get(session, f"{GAMMA_HOST}/events", params=params)
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("events") or data.get("data") or []
    return []


def _parse_event(event: dict, cutoff_ts: int, symbols_filter: list[str]) -> Optional[dict]:
    """
    Try to extract one market record from a gamma event dict.
    Returns None if the event should be skipped.
    """
    slug = (event.get("slug") or "").lower()

    # Fast reject: skip markers (5m/15m/4h variants)
    if has_skip_marker(slug):
        return None

    # Primary symbol detection via slug prefix
    symbol = slug_to_symbol(slug)

    # Fallback: keyword detection if prefix doesn't match
    if not symbol:
        title = event.get("title") or event.get("description") or ""
        if not is_updown_market(slug, title):
            return None
        symbol = detect_symbol(title) or detect_symbol(slug)

    if not symbol or symbol not in symbols_filter:
        return None

    # Markets are nested in event["markets"]; use first market
    markets_list = event.get("markets") or []
    if not markets_list:
        return None
    mkt = markets_list[0]

    # Date filter — prefer market dates, fall back to event dates
    end_date_str = (
        mkt.get("endDate") or mkt.get("end_date") or
        event.get("endDate") or event.get("end_date") or ""
    )
    end_ts = parse_ts(end_date_str)
    if end_ts and end_ts < cutoff_ts:
        return None  # too old

    start_date_str = (
        mkt.get("startDate") or mkt.get("start_date") or
        event.get("startDate") or event.get("start_date") or ""
    )
    start_ts = parse_ts(start_date_str)

    cond_id = (
        mkt.get("conditionId") or mkt.get("condition_id") or
        event.get("conditionId") or event.get("condition_id") or ""
    )
    if not cond_id:
        return None

    question = (
        mkt.get("question") or mkt.get("title") or
        event.get("title") or event.get("description") or ""
    )

    outcomes   = _parse_json_field(mkt.get("outcomes") or [])
    prices_raw = _parse_json_field(mkt.get("outcomePrices") or [])
    token_ids  = _parse_json_field(mkt.get("clobTokenIds") or [])

    token_id_up, token_id_down = extract_tokens(outcomes, token_ids)
    resolved_out = resolve_outcome(outcomes, prices_raw)

    return {
        "condition_id":     cond_id,
        "slug":             event.get("slug") or "",
        "symbol":           symbol,
        "question":         question,
        "start_ts":         start_ts,
        "end_ts":           end_ts,
        "resolved_outcome": resolved_out,
        "token_id_up":      token_id_up,
        "token_id_down":    token_id_down,
    }


async def discover_markets(
    session: aiohttp.ClientSession,
    days_back: int,
    symbols_filter: list[str],
    verbose: bool = False,
) -> list[dict]:
    """
    Paginate through gamma /events (closed=true), filter for UP/DOWN hourly
    crypto events within the date window. Returns parsed market dicts.
    """
    now_ts = int(time.time())
    cutoff_ts = now_ts - days_back * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    log.info(f"Discovering markets via /events: last {days_back} days (cutoff {cutoff_iso})")

    results: list[dict] = []
    seen_cids: set[str] = set()
    offset = 0
    page = 0

    while True:
        page += 1
        raw = await fetch_events_page(
            session, offset=offset, end_date_min=cutoff_iso
        )
        if not raw:
            log.debug(f"Empty page at offset {offset} — stopping")
            break

        if verbose:
            log.debug(f"Page {page}: received {len(raw)} raw events")

        found_in_page = 0
        all_too_old = True

        for evt in raw:
            # Check event-level endDate for early termination
            evt_end_str = (
                evt.get("endDate") or evt.get("end_date") or ""
            )
            evt_end_ts = parse_ts(evt_end_str)
            if evt_end_ts and evt_end_ts >= cutoff_ts:
                all_too_old = False

            parsed = _parse_event(evt, cutoff_ts, symbols_filter)
            if parsed is None:
                continue

            cid = parsed["condition_id"]
            if cid in seen_cids:
                continue
            seen_cids.add(cid)

            results.append(parsed)
            found_in_page += 1

        log.debug(f"Page {page} offset {offset}: {found_in_page} matching markets (page total {len(raw)})")

        if len(raw) < GAMMA_PAGE_LIMIT:
            break  # last page

        if all_too_old:
            log.debug(f"All events on page {page} older than cutoff — stopping")
            break

        offset += len(raw)
        await asyncio.sleep(REQ_DELAY)

    log.info(f"Discovery complete: {len(results)} markets found")
    return results


# ---------------------------------------------------------------------------
# Trade fetch (Data API)
# ---------------------------------------------------------------------------

async def fetch_trades_for_market(
    session: aiohttp.ClientSession,
    condition_id: str,
    token_id_up: Optional[str],
    token_id_down: Optional[str],
    verbose: bool = False,
) -> list[dict]:
    """
    Paginate data-api /trades for one market (by conditionId).
    Returns list of normalized trade dicts.
    """
    all_trades: list[dict] = []
    offset = 0
    now_ts = int(time.time())

    while True:
        params = {
            "market": condition_id,
            "limit":  TRADE_PAGE_LIMIT,
            "offset": offset,
        }
        url = f"{DATA_API_HOST}/trades"
        data = await _get(session, url, params=params)

        if data is None:
            log.warning(f"  Null response for {condition_id[:12]} offset {offset}")
            break

        # data-api may return list or {"data": [...], ...}
        if isinstance(data, dict):
            rows = data.get("data") or data.get("trades") or data.get("results") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []

        if not rows:
            break

        for r in rows:
            # handle both camelCase and snake_case field names
            ts = parse_ts(
                r.get("timestamp") or r.get("created_at") or
                r.get("createdAt") or r.get("time")
            )
            price = None
            size  = None
            try:
                price = float(r.get("price") or 0)
                size  = float(
                    r.get("size") or r.get("amount") or
                    r.get("usdcSize") or r.get("usdc_size") or 0
                )
            except (TypeError, ValueError):
                pass

            side = (r.get("side") or r.get("takerSide") or "").upper()
            if side not in ("BUY", "SELL"):
                side = None

            raw_outcome = (
                r.get("outcome") or r.get("asset_outcome") or ""
            ).strip()

            # data-api field for token_id is "asset" (confirmed from live probe)
            token_id = (
                r.get("asset") or r.get("asset_id") or r.get("assetId") or
                r.get("token_id") or r.get("tokenId") or ""
            )

            # Derive outcome from token_id → Up/Down mapping
            if token_id:
                mapped = outcome_for_token(token_id, token_id_up, token_id_down)
                outcome = mapped or raw_outcome or None
            else:
                outcome = raw_outcome or None

            # Normalize "Yes"→"Up", "No"→"Down" if Polymarket uses Yes/No
            if outcome and outcome.lower() in ("yes", "up"):
                outcome = "Up"
            elif outcome and outcome.lower() in ("no", "down"):
                outcome = "Down"
            else:
                outcome = outcome or None

            all_trades.append({
                "condition_id": condition_id,
                "token_id":     token_id or None,
                "timestamp":    ts,
                "price":        price,
                "size":         size,
                "side":         side,
                "outcome":      outcome,
                "fetched_at":   now_ts,
            })

        if verbose:
            log.debug(f"  {condition_id[:12]}: offset {offset} -> {len(rows)} trades")

        if len(rows) < TRADE_PAGE_LIMIT:
            break  # last page

        offset += len(rows)
        await asyncio.sleep(REQ_DELAY)

    return all_trades


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def upsert_market(conn: sqlite3.Connection, m: dict, dry_run: bool) -> bool:
    if dry_run:
        return True
    try:
        conn.execute(
            """INSERT INTO markets
               (condition_id, slug, symbol, question, start_ts, end_ts,
                resolved_outcome, token_id_up, token_id_down, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(condition_id) DO NOTHING""",
            (
                m["condition_id"], m["slug"], m["symbol"], m["question"],
                m["start_ts"], m["end_ts"], m["resolved_outcome"],
                m["token_id_up"], m["token_id_down"], int(time.time()),
            ),
        )
        conn.commit()
        return True
    except Exception as e:
        log.error(f"DB market insert error ({m['condition_id'][:12]}): {e}")
        return False


def insert_trades(
    conn: sqlite3.Connection,
    trades: list[dict],
    dry_run: bool,
) -> int:
    if dry_run or not trades:
        return len(trades) if dry_run else 0
    inserted = 0
    try:
        for t in trades:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO trades
                       (condition_id, token_id, timestamp, price, size, side, outcome, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        t["condition_id"], t.get("token_id"),
                        t["timestamp"], t["price"], t["size"],
                        t["side"], t["outcome"], t["fetched_at"],
                    ),
                )
                inserted += conn.execute("SELECT changes()").fetchone()[0]
            except Exception as e:
                log.debug(f"Trade insert skip: {e}")
        conn.commit()
    except Exception as e:
        log.error(f"DB trades batch error: {e}")
    return inserted


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def run(
    days_back: int = 30,
    symbols_filter: list[str] | None = None,
    skip_existing: bool = True,
    markets_only: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    db_path: Path = DB_PATH,
) -> None:
    if symbols_filter is None:
        symbols_filter = SYMBOLS

    t_start = time.time()

    log.info(f"=== Polymarket Historical Fetcher ===")
    log.info(f"Days back     : {days_back}")
    log.info(f"Symbols       : {', '.join(symbols_filter)}")
    log.info(f"Skip existing : {skip_existing}")
    log.info(f"Markets only  : {markets_only}")
    log.info(f"Dry run       : {dry_run}")
    log.info(f"DB path       : {db_path}")

    # DB setup
    conn: Optional[sqlite3.Connection] = None
    if not dry_run:
        if db_path.exists():
            print(
                f"\nDatabase exists: {db_path}\n"
                "Append new data? [y/N] ",
                end="",
                flush=True,
            )
            ans = sys.stdin.readline().strip().lower()
            if ans not in ("y", "yes"):
                print("Aborted.")
                return
        conn = open_db(db_path)
        log.info("DB ready.")
    else:
        log.info("DRY RUN — no DB writes.")

    already_fetched_markets: set[str] = set()
    already_fetched_trades: set[str] = set()
    if conn and skip_existing:
        already_fetched_markets = existing_condition_ids(conn)
        already_fetched_trades  = existing_trade_cids(conn)
        log.info(
            f"Existing DB: {len(already_fetched_markets)} markets, "
            f"{len(already_fetched_trades)} with trades"
        )

    # Market discovery
    headers = {"Accept": "application/json", "User-Agent": "polymarket-historical-fetcher/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        markets = await discover_markets(
            session, days_back=days_back,
            symbols_filter=symbols_filter, verbose=verbose,
        )

    if not markets:
        print("No markets found — check date range or API availability.")
        if conn:
            conn.close()
        return

    # Write market rows
    new_markets = [
        m for m in markets
        if m["condition_id"] not in already_fetched_markets
    ]
    log.info(
        f"Markets: {len(markets)} total, {len(new_markets)} new "
        f"(skip_existing={skip_existing})"
    )

    for m in new_markets:
        upsert_market(conn, m, dry_run)

    if markets_only:
        _print_summary(markets, new_markets, [], 0, t_start, db_path, dry_run)
        if conn:
            conn.close()
        return

    # Trade fetch — only markets not already fetched (or all if skip_existing=False)
    markets_to_fetch = [
        m for m in markets
        if skip_existing is False
        or m["condition_id"] not in already_fetched_trades
    ]
    log.info(
        f"Fetching trades for {len(markets_to_fetch)} markets "
        f"(skip_existing={skip_existing})"
    )

    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    total_trades_inserted = 0
    processed = 0

    async def fetch_one(m: dict) -> int:
        nonlocal processed, total_trades_inserted
        async with sem:
            cid = m["condition_id"]
            async with aiohttp.ClientSession(headers=headers) as session2:
                trades = await fetch_trades_for_market(
                    session2,
                    condition_id=cid,
                    token_id_up=m["token_id_up"],
                    token_id_down=m["token_id_down"],
                    verbose=verbose,
                )
            inserted = insert_trades(conn, trades, dry_run)
            processed += 1
            total_trades_inserted += inserted
            if processed % 10 == 0 or processed == len(markets_to_fetch):
                pct = 100 * processed / len(markets_to_fetch)
                log.info(
                    f"Progress: {processed}/{len(markets_to_fetch)} markets "
                    f"({pct:.0f}%) | trades so far: {total_trades_inserted}"
                )
            await asyncio.sleep(REQ_DELAY)
            return inserted

    tasks = [fetch_one(m) for m in markets_to_fetch]
    await asyncio.gather(*tasks)

    _print_summary(
        markets, new_markets, markets_to_fetch,
        total_trades_inserted, t_start, db_path, dry_run, conn,
    )

    if conn:
        conn.close()


def _print_summary(
    markets: list[dict],
    new_markets: list[dict],
    fetched: list[dict],
    trades_inserted: int,
    t_start: float,
    db_path: Path,
    dry_run: bool,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    elapsed = time.time() - t_start
    m_min, m_sec = divmod(int(elapsed), 60)

    sym_counts: dict[str, int] = {}
    for m in markets:
        sym_counts[m["symbol"]] = sym_counts.get(m["symbol"], 0) + 1

    # date range
    end_tss = [m["end_ts"] for m in markets if m.get("end_ts")]
    if end_tss:
        date_min = datetime.fromtimestamp(min(end_tss), tz=timezone.utc).strftime("%Y-%m-%d")
        date_max = datetime.fromtimestamp(max(end_tss), tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        date_min = date_max = "N/A"

    # DB size
    db_mb = ""
    if db_path.exists():
        db_mb = f" ({db_path.stat().st_size / 1024 / 1024:.2f} MB)"

    total_trades_in_db = 0
    if conn:
        try:
            total_trades_in_db = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        except Exception:
            pass

    print()
    print("=" * 55)
    print("FETCH COMPLETE" + (" [DRY RUN]" if dry_run else ""))
    print("=" * 55)
    print(f"Markets found    : {len(markets)} ({len(new_markets)} new)")
    sym_str = " | ".join(f"{s}={sym_counts.get(s,0)}" for s in SYMBOLS)
    print(f"Symbols          : {sym_str}")
    print(f"Date range       : {date_min} to {date_max}")
    if fetched:
        print(f"Trades inserted  : {trades_inserted:,}")
        if conn:
            print(f"Total in DB      : {total_trades_in_db:,}")
    print(f"Storage          : {db_path}{db_mb}")
    print(f"Time elapsed     : {m_min}m {m_sec}s")
    print("=" * 55)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch historical Polymarket UP/DOWN hourly trade data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test -- last 7 days, metadata only
  python -m script.fetch_historical --days 7 --markets-only

  # Full fetch -- last 30 days, all symbols
  python -m script.fetch_historical --days 30

  # BTC and ETH only, verbose
  python -m script.fetch_historical --days 14 --symbols BTC,ETH --verbose

  # Dry run (no DB writes)
  python -m script.fetch_historical --days 7 --dry-run
""",
    )
    parser.add_argument("--days",    type=int,   default=30,
                        help="Days back to fetch (default 30)")
    parser.add_argument("--symbols", type=str,   default=",".join(SYMBOLS),
                        help="Comma-separated symbols (default: all 6)")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="Skip markets already in DB (default True)")
    parser.add_argument("--no-skip-existing", dest="skip_existing",
                        action="store_false",
                        help="Re-fetch everything even if already in DB")
    parser.add_argument("--markets-only", action="store_true",
                        help="Fetch market metadata, skip trades")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Simulate without writing to DB")
    parser.add_argument("--verbose",  action="store_true",
                        help="Detailed per-page and per-market logging")
    parser.add_argument("--db",       type=str,   default=str(DB_PATH),
                        help=f"DB path (default: {DB_PATH})")

    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    invalid = [s for s in symbols if s not in SYMBOLS]
    if invalid:
        parser.error(f"Unknown symbols: {invalid}. Valid: {SYMBOLS}")

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(
        run(
            days_back      = args.days,
            symbols_filter = symbols,
            skip_existing  = args.skip_existing,
            markets_only   = args.markets_only,
            dry_run        = args.dry_run,
            verbose        = args.verbose,
            db_path        = Path(args.db),
        )
    )


if __name__ == "__main__":
    main()
