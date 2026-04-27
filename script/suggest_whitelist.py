"""
script/suggest_whitelist.py
============================
Helper untuk populate `data/political_whitelist.json`.

Flow:
1. Fetch active political markets dari Polymarket (Gamma API).
2. Untuk setiap market, cari best-match Kalshi event berdasarkan similarity.
3. Output CSV `data/whitelist_candidates.csv` dengan kandidat pair + metadata.

Kamu review CSV manual, copy condition_id -> kalshi_event_ticker yang BENAR
ke `data/political_whitelist.json` di field "markets".

Tidak ada auto-promote — tujuan whitelist justru menghindari fuzzy false match,
jadi semua entry harus diverifikasi mata manusia.

Usage:
    python -m script.suggest_whitelist
    python -m script.suggest_whitelist --limit 200 --min-sim 0.5
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from pathlib import Path

import aiohttp

# Repo root ke sys.path biar bisa di-run sebagai script biasa juga
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.api.gamma_client import GammaClient
from src.api.metaculus_client import _similarity, _DEFAULT_HEADERS, _KALSHI_BASE
from src.logic.political_mispricing import is_political_market

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_OUT_CSV    = _ROOT / "data" / "whitelist_candidates.csv"
_WL_JSON    = _ROOT / "data" / "political_whitelist.json"
_KALSHI_CACHE_FILE = _ROOT / "data" / ".kalshi_cache.json"
_KALSHI_CACHE_TTL  = 3600  # 1 jam — Kalshi events nggak berubah cepat

_RELEVANT_CATEGORIES = {
    "Politics", "Elections", "World",
    "Climate and Weather", "Science and Technology", "Economics",
}


def _load_existing_whitelist() -> set[str]:
    """Return set dari condition_id yang sudah ada di whitelist."""
    try:
        with open(_WL_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set((data.get("markets") or {}).keys())
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _read_kalshi_disk_cache(max_pages: int) -> list[dict] | None:
    """Return cached Kalshi markets dari disk kalau masih fresh.
    Kalau pages_done < max_pages requested, tetap pakai cache + warn user.
    """
    try:
        with open(_KALSHI_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if time.time() - payload.get("ts", 0) > _KALSHI_CACHE_TTL:
        return None
    cached_pages = payload.get("max_pages", 0)
    flat = payload.get("flat") or []
    if cached_pages < max_pages:
        print(
            f"  WARN: cache cuma punya {cached_pages} page(s), kamu minta {max_pages}. "
            f"Pakai cache yang ada. Hapus {_KALSHI_CACHE_FILE.name} buat refresh full."
        )
    return flat


def _write_kalshi_disk_cache(flat: list[dict], max_pages: int) -> None:
    try:
        with open(_KALSHI_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "max_pages": max_pages, "flat": flat}, f)
    except Exception as e:
        print(f"  WARN: gagal write cache: {e}")


async def _fetch_kalshi_with_progress(
    session: aiohttp.ClientSession,
    max_pages: int,
) -> list[dict]:
    """Fetch Kalshi events dengan progress per-page + disk cache.
    Cache di-write SETELAH SETIAP PAGE — jadi kalau user Ctrl+C atau API
    error, partial data tetap kepake (next run resume dari cache).
    """
    cached = _read_kalshi_disk_cache(max_pages)
    if cached is not None:
        print(f"  -> {len(cached)} markets (from disk cache, max age 1h)")
        return cached

    flat: list[dict] = []
    cursor = ""
    pages_done = 0
    try:
        for page in range(max_pages):
            t0 = time.time()
            params = {
                "limit":              "200",
                "status":             "open",
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                async with session.get(
                    f"{_KALSHI_BASE}/events",
                    params=params,
                    headers=_DEFAULT_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except Exception as e:
                print(f"  page {page+1} ERROR: {type(e).__name__}: {e}")
                break

            events = data.get("events", []) or []
            added  = 0
            for ev in events:
                if ev.get("category") not in _RELEVANT_CATEGORIES:
                    continue
                ev_title  = ev.get("title") or ""
                markets   = ev.get("markets") or []
                is_binary = len(markets) == 1
                for m in markets:
                    if m.get("status") != "active":
                        continue
                    flat.append({
                        "event_title":  ev_title,
                        "event_ticker": ev.get("event_ticker"),
                        "category":     ev.get("category"),
                        "is_binary":    is_binary,
                        "market":       m,
                    })
                    added += 1

            pages_done = page + 1
            print(f"  page {pages_done}/{max_pages}: {len(events)} events, {added} relevant ({time.time()-t0:.1f}s)")
            # Incremental save — kalau interrupted, partial data preserved
            _write_kalshi_disk_cache(flat, pages_done)

            cursor = data.get("cursor", "") or ""
            if not cursor or not events:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n  Interrupted — saving {len(flat)} markets dari {pages_done} page(s)")
        _write_kalshi_disk_cache(flat, pages_done)
        raise

    print(f"  -> {len(flat)} Kalshi markets cached to disk")
    return flat


async def _best_kalshi_match(
    query: str,
    kalshi_flat: list[dict],
    min_sim: float,
) -> dict | None:
    """Cari top Kalshi candidate untuk query. Return dict atau None."""
    best, best_score = None, 0.0
    for entry in kalshi_flat:
        m = entry["market"]
        ev_title = entry["event_title"]
        if entry["is_binary"]:
            cmp_title = ev_title
        else:
            outcome = m.get("yes_sub_title") or m.get("title") or ""
            cmp_title = f"{ev_title} - {outcome}"
        score = _similarity(query, cmp_title)
        if score > best_score:
            best_score = score
            best = entry
            best["_cmp_title"] = cmp_title
            best["_score"] = score
    if best is None or best_score < min_sim:
        return None
    return best


async def main(limit: int, min_sim: float, min_volume: float, pages: int) -> int:
    existing = _load_existing_whitelist()
    print(f"Whitelist existing: {len(existing)} entries (skip)")

    gamma = GammaClient()

    async with aiohttp.ClientSession() as session:
        print("Fetch Polymarket markets...")
        markets = await gamma.aget_markets(
            session, limit=limit, active=True, closed=False
        )
        print(f"  -> {len(markets)} markets")

        print(f"Fetch Kalshi events (max {pages} pages, ~10s/page)...")
        kalshi_flat = await _fetch_kalshi_with_progress(session, pages)

        rows: list[dict] = []
        for m in markets:
            condition_id = m.get("conditionId") or m.get("id") or ""
            question     = m.get("question") or m.get("title") or ""
            if not condition_id or not question:
                continue
            if condition_id in existing:
                continue
            if not is_political_market(question):
                continue

            try:
                volume = float(m.get("volume", 0) or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < min_volume:
                continue

            match = await _best_kalshi_match(question, kalshi_flat, min_sim)
            if match is None:
                rows.append({
                    "condition_id":     condition_id,
                    "polymarket_q":     question[:120],
                    "polymarket_vol":   round(volume),
                    "kalshi_ticker":    "",
                    "kalshi_title":     "",
                    "similarity":       "",
                    "kalshi_category":  "",
                    "verdict":          "no-match",
                })
                continue

            km = match["market"]
            try:
                k_vol = int(float(km.get("volume_fp") or 0))
            except (TypeError, ValueError):
                k_vol = 0
            rows.append({
                "condition_id":    condition_id,
                "polymarket_q":    question[:120],
                "polymarket_vol":  round(volume),
                "kalshi_ticker":   match.get("event_ticker") or "",
                "kalshi_title":    (match["_cmp_title"] or "")[:120],
                "similarity":      round(match["_score"], 2),
                "kalshi_category": match.get("category") or "",
                "kalshi_vol":      k_vol,
                "verdict":         "REVIEW",
            })

    # Sort: REVIEW first by similarity desc, no-match last
    rows.sort(
        key=lambda r: (
            0 if r["verdict"] == "REVIEW" else 1,
            -float(r["similarity"]) if r["similarity"] != "" else 0.0,
        )
    )

    fieldnames = [
        "verdict", "similarity", "condition_id",
        "polymarket_q", "polymarket_vol",
        "kalshi_ticker", "kalshi_title", "kalshi_category", "kalshi_vol",
    ]
    _OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(_OUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    n_review = sum(1 for r in rows if r["verdict"] == "REVIEW")
    n_nomatch = sum(1 for r in rows if r["verdict"] == "no-match")
    print()
    print(f"Output: {_OUT_CSV}")
    print(f"  {n_review} kandidat REVIEW (similarity >= {min_sim})")
    print(f"  {n_nomatch} no-match (skip atau cari di sumber lain)")
    print()
    print("Next:")
    print(f"  1. Buka {_OUT_CSV.name}, sort by similarity desc.")
    print(f"  2. Verifikasi mata: kalshi_title benar-benar = polymarket_q?")
    print(f"  3. Copy pair yang valid ke {_WL_JSON.name} di field 'markets'.")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--limit",      type=int,   default=200,
                   help="Jumlah Polymarket market di-fetch (default 200)")
    p.add_argument("--min-sim",    type=float, default=0.50,
                   help="Similarity minimum untuk dianggap kandidat (default 0.50)")
    p.add_argument("--min-volume", type=float, default=5000.0,
                   help="Volume minimum Polymarket (default 5000)")
    p.add_argument("--pages",      type=int,   default=5,
                   help="Max page Kalshi (200/page, default 5 = ~50s. Set 10 untuk full coverage)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(asyncio.run(main(args.limit, args.min_sim, args.min_volume, args.pages)))
