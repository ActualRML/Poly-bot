"""
src/logic/political_mispricing.py
====================================
Helper untuk political/event market strategy.

Base probability di-blend dari multiple sources paralel:
- Kalshi    (real money, regulated US) — confidence 0.85
- Manifold  (play money)                — confidence 0.60

Berbeda dari crypto strategy yang pakai log-normal model.
"""

import json
import logging
from pathlib import Path

import aiohttp

from src.api.metaculus_client import metaculus_client
from src.logic.mispricing import BaseRate

logger = logging.getLogger(__name__)

# Spread max antar sumber sebelum dianggap noisy / salah match.
# CLAUDE.md: Kalshi vs Manifold beda > 20% → skip.
_MAX_SOURCE_DISAGREEMENT = 0.20

# Whitelist Polymarket↔Kalshi mapping. Strict mode aktif kalau file punya
# minimal 1 entry; kosong = permissive (warn) untuk backward compat.
_WHITELIST_PATH = Path(__file__).resolve().parents[2] / "data" / "political_whitelist.json"
_whitelist_cache: dict[str, str] | None = None


def _load_whitelist() -> dict[str, str]:
    """Load whitelist dari file, cached. Return empty dict kalau file tidak ada / corrupt.
    Log status sekali pas load pertama (info kalau ada isinya, warning kalau kosong).
    """
    global _whitelist_cache
    if _whitelist_cache is not None:
        return _whitelist_cache
    try:
        with open(_WHITELIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _whitelist_cache = data.get("markets", {}) or {}
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"[POLITICAL] Whitelist tidak bisa di-load ({e}); strict mode OFF")
        _whitelist_cache = {}

    if _whitelist_cache:
        logger.info(
            f"[POLITICAL] Whitelist loaded: {len(_whitelist_cache)} entries — strict mode ON"
        )
    else:
        logger.warning(
            "[POLITICAL] Whitelist kosong — strict mode OFF. "
            "Risiko false match tinggi; isi data/political_whitelist.json sebelum live."
        )
    return _whitelist_cache


def _is_whitelisted(condition_id: str | None) -> tuple[bool, bool]:
    """
    Return (allowed, strict_mode_active).
    - strict_mode_active=True kalau whitelist punya >=1 entry.
    - allowed=True kalau strict OFF, atau condition_id ada di whitelist.
    """
    wl = _load_whitelist()
    if not wl:
        return True, False
    if not condition_id:
        return False, True
    return (condition_id in wl), True

# Keywords yang menandakan market adalah CRYPTO PRICE market
# Market ini sudah di-handle crypto strategy, skip di sini
_CRYPTO_PRICE_KEYWORDS = [
    "bitcoin", "btc", "ethereum", " eth ", "ether",
    "solana", " sol ", "xrp", "ripple", "doge", "dogecoin",
    " bnb ", "binance coin", "reach $", "above $", "below $",
    "dip to $", "drop to $",
]

# Keywords sports/eSports — exclude dari political pipeline.
# Sports butuh model lain (form, odds, head-to-head), bukan base rate political.
_SPORTS_KEYWORDS = [
    # Leagues / tournaments
    "nba", "nfl", "mlb", "nhl", "fifa", "uefa", "champions league",
    "premier league", "la liga", "bundesliga", "serie a", "ligue 1",
    "world cup", "eurovision", "olympics", "wimbledon", "french open",
    "us open", "australian open", "madrid open", "indian premier league",
    "pakistan super league", "ipl", "psl",
    # eSports
    "dota", "league of legends", " lol ", "lol:", "counter-strike",
    "valorant", "csgo", "cs:go", "esports",
    # Sport keywords umum
    "playoffs", "finals", "championship", "season",
    # Match patterns "TeamA vs. TeamB"
    " vs. ", " vs ",
    # Match outcome patterns
    "end in a draw", "win on 20", "o/u 2.5", "spread:", "handicap",
    " bo3 ", " bo5 ", "(bo3)", "(bo5)",
    # Event teams (umum di Polymarket)
    "fc ", "afc ", "cf ", "fbk", "psg", "real madrid", "barcelona",
    "manchester", "chelsea", "liverpool", "arsenal", "juventus",
    "bayern", "lakers", "celtics", "knicks", "warriors", "rockets",
    "nuggets", "heat", "bucks", "76ers", "pistons", "magic",
    "thunder", "spurs", "cavaliers", "mavericks", "timberwolves",
    "trail blazers", "raptors", "rangers", "yankees", "red sox",
    "phillies", "braves", "rockies", "mets", "giants", "dodgers",
]

# Confidence per source berdasarkan kualitas pasar
_SOURCE_CONFIDENCE = {
    "kalshi":   0.85,   # regulated US real money
    "manifold": 0.60,   # play money — sinyalnya lebih lemah
}


def is_political_market(question: str) -> bool:
    """Return True kalau market layak masuk political pipeline.
    Exclude crypto price market (handled by crypto strategy) dan sports
    (butuh model berbeda, bukan base rate political).
    """
    q = question.lower()
    if any(kw in q for kw in _CRYPTO_PRICE_KEYWORDS):
        return False
    if any(kw in q for kw in _SPORTS_KEYWORDS):
        return False
    return True


def _confidence_for(source: str, num_predictors: int) -> float:
    """
    Confidence final = base confidence per source × adjustment kalau
    sample size kecil. Kalshi pakai volume, jadi adjustment beda.
    """
    base = _SOURCE_CONFIDENCE.get(source, 0.5)
    if source == "kalshi":
        # Volume kecil → kurangi confidence sampai 0.6× base
        if num_predictors < 100:
            return round(base * 0.7, 2)
        if num_predictors < 1000:
            return round(base * 0.85, 2)
        return base
    if source == "manifold":
        # Trader kecil → kurangi confidence
        if num_predictors < 20:
            return round(base * 0.7, 2)
        if num_predictors < 100:
            return round(base * 0.85, 2)
        return base
    return base


async def get_political_base_rates(
    question: str,
    session: aiohttp.ClientSession,
    condition_id: str | None = None,
) -> list[BaseRate]:
    """
    Fetch base probability dari Kalshi + Manifold paralel.

    Returns:
        list[BaseRate] dengan satu entry per sumber yang ada match-nya.
        [] kalau semua sumber tidak punya match, source disagree > 20%,
        atau condition_id tidak ada di whitelist (strict mode).
    """
    from src.utils.config import config

    allowed, _strict = _is_whitelisted(condition_id)
    if not allowed:
        logger.debug(
            f"[POLITICAL] Skip '{question[:45]}' — condition_id "
            f"{(condition_id or '<none>')[:14]}... not in whitelist"
        )
        return []

    min_sim = config.METACULUS_MATCH_SCORE  # nama config legacy, dipakai untuk semua source

    matches = await metaculus_client.search_all(
        query=question,
        session=session,
        min_similarity=min_sim,
    )
    if not matches:
        return []

    base_rates: list[BaseRate] = []
    for m in matches:
        source = m.get("source", "manifold")
        num    = int(m.get("num_predictors") or 0)
        prob   = float(m["probability"])
        title  = m.get("title", "")

        base_rates.append(BaseRate(
            source=source,
            rate=prob,
            confidence=_confidence_for(source, num),
            sample_size=num,
            notes=f"matched: {title[:60]}",
        ))

    # Disagreement check — kalau Kalshi vs Manifold (atau >2 sumber) beda jauh,
    # signal noisy / salah match → skip.
    if len(base_rates) > 1:
        rates = [br.rate for br in base_rates]
        spread = max(rates) - min(rates)
        if spread > _MAX_SOURCE_DISAGREEMENT:
            logger.info(
                f"[POLITICAL] Skip '{question[:45]}' — sources disagree by "
                f"{spread:.0%} (> {_MAX_SOURCE_DISAGREEMENT:.0%})"
            )
            return []

    sources = ", ".join(br.source for br in base_rates)
    logger.info(f"[POLITICAL] '{question[:45]}' -> {len(base_rates)} sources: {sources}")
    return base_rates
