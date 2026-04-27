"""
src/backtest/question_parser.py
===============================
Parse pertanyaan market Polymarket → struct asset/target/direction/model.

Filter ketat: hanya market dengan single-target price prediction yang lulus.
Market range/candle/up-or-down → di-skip (tidak bisa kita model).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


CRYPTO_KEYWORDS: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether"],
    "SOL": ["solana"],
}

# Sanity range — kalau target di luar range ini, classifier salah.
ASSET_PRICE_RANGE: dict[str, tuple[float, float]] = {
    "BTC": (1_000, 1_000_000),
    "ETH": (100, 50_000),
    "SOL": (5, 5_000),
}

DIRECTION_DOWN = ["dip", "drop", "fall", "below", "↓", "under "]

# Regex price target — urutan dari paling spesifik ke generic
PRICE_PATTERNS = [
    r'\$([0-9]{1,3}(?:,[0-9]{3})+)',     # $77,000
    r'\$([0-9]+(?:\.[0-9]+)?)[kK]\b',     # $77k
    r'\$([0-9]{4,})',                     # $77000
    r'\$([0-9]+(?:\.[0-9]+)?)',           # $1.40 / $140
    r'[↑↓]\s*\$?([0-9]{1,3}(?:,[0-9]{3})+)',
    r'[↑↓]\s*\$?([0-9]+(?:\.[0-9]+)?)[kK]\b',
    r'[↑↓]\s*\$?([0-9]+(?:\.[0-9]+)?)',
]

# Skip kalau question matches salah satu pattern ini — gak bisa kita model
SKIP_PATTERNS = [
    r'\bbetween\b',
    r'\brange\b',
    r'\bdip to\b',
    r'\bup or down\b',
    r'\bcandle change\b',
    r'\bdaily candle\b',
    r'\bgreater dip\b',
    r'\bnew all[- ]?time high\b',     # ATH markets — terlalu generic, target moving
    r'\bgreater\s+(?:gain|loss)\b',
    r'\bhighest\b.*\bbefore\b',       # highest X before Y
    r'\bvs\.?\s+ETH\b',                # cross-asset (BTC vs ETH dominance)
    r'\bvs\.?\s+BTC\b',
    r'\bdominance\b',
    r'\bmarket cap\b',
    r'\bfloor price\b',                # NFT market
    # Multivariate "X or Y first" — bukan single-barrier
    r'\$[0-9,]+(?:[kK])?\s+or\s+\$[0-9,]+(?:[kK])?\s+first\b',
    r'\bhit\s+\$[0-9,]+(?:[kK])?\s+or\s+\$[0-9,]+',
    r'\breach\s+\$[0-9,]+(?:[kK])?\s+or\s+\$[0-9,]+',
]


@dataclass
class ParsedMarket:
    asset: str
    target_price: float
    direction: str            # "above" or "below"
    use_barrier: bool         # True = any-touch, False = at-expiry
    raw_question: str


def _classify_asset(question_lower: str) -> Optional[str]:
    """
    Stricter: harus match keyword DAN tidak cuma sebagai denomination.
    Contoh skip: "30 ETH" (target dalam ETH, bukan harga ETH).
    """
    # Cek bentuk denominasi: "<number> <ASSET>" — ini biasanya cross-asset
    for asset in CRYPTO_KEYWORDS:
        denom_pattern = rf'\b\d+(?:\.\d+)?\s*{asset.lower()}\b'
        if re.search(denom_pattern, question_lower):
            return None

    for asset, keywords in CRYPTO_KEYWORDS.items():
        for kw in keywords:
            # Word boundary biar "ether" ga match "etherium" di nama lain
            pattern = rf'\b{re.escape(kw)}\b'
            if re.search(pattern, question_lower):
                return asset
    return None


def _extract_price_target(question: str) -> Optional[float]:
    for pattern in PRICE_PATTERNS:
        match = re.search(pattern, question)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        # Cek 'k' multiplier
        matched_text = question[match.start():match.end()].lower()
        if 'k' in matched_text:
            value *= 1000
        return value
    return None


def parse_market(question: str) -> Optional[ParsedMarket]:
    """
    Parse satu question. Return ParsedMarket kalau sukses, None kalau di-skip.

    Rules:
    - Match salah satu CRYPTO_KEYWORDS
    - Tidak match SKIP_PATTERNS
    - Punya price target valid dalam ASSET_PRICE_RANGE asset terkait
    """
    q = question.lower()

    for skip_pat in SKIP_PATTERNS:
        if re.search(skip_pat, q):
            return None

    asset = _classify_asset(q)
    if asset is None:
        return None

    target = _extract_price_target(question)
    if target is None:
        return None

    lo, hi = ASSET_PRICE_RANGE[asset]
    if not (lo <= target <= hi):
        return None

    direction = "below" if any(k in q for k in DIRECTION_DOWN) else "above"

    # Heuristik dari main.py:
    # "above $X on <date>" → at-expiry (harga di hari tertentu)
    # "reach $X by <date>" / "hit $X" → barrier (any-touch)
    use_barrier = " on " not in q

    return ParsedMarket(
        asset         = asset,
        target_price  = target,
        direction     = direction,
        use_barrier   = use_barrier,
        raw_question  = question,
    )


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    cases = [
        "Will Bitcoin reach $100,000 by December 31, 2026?",
        "Will Ethereum hit $5,000 by end of year?",
        "Will Solana be above $250 on December 31, 2025?",
        "Will Bitcoin dip to $50,000?",
        "Will CryptoPunks floor price reach 30 ETH before 2027?",  # SKIP — denom
        "Bitcoin 9% daily candle change in 2026?",                  # SKIP — candle
        "Will BTC reach a new all-time high?",                      # SKIP — ATH
        "BTC vs ETH dominance Q4 2026?",                            # SKIP — cross-asset
        "Will Bitcoin price be between $80k and $100k?",            # SKIP — range
        "ETH up or down on Friday?",                                # SKIP — up or down
    ]
    for q in cases:
        result = parse_market(q)
        if result:
            print(f"✓ {q}")
            print(f"   asset={result.asset} target=${result.target_price:,.0f} "
                  f"dir={result.direction} barrier={result.use_barrier}")
        else:
            print(f"✗ SKIP: {q}")
