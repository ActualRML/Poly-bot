from __future__ import annotations

import re

_SYMBOL_KEYWORDS = {
    "BTC":  ("bitcoin", "btc"),
    "ETH":  ("ethereum", "eth"),
    "SOL":  ("solana", "sol"),
    "XRP":  ("xrp",),
    "DOGE": ("dogecoin", "doge"),
    "BNB":  ("bnb",),
}


def detect_symbol_from_question(question: str) -> str:
    if not question:
        return "UNKNOWN"
    q_lower = question.lower()
    for sym, keywords in _SYMBOL_KEYWORDS.items():
        for kw in keywords:
            if kw in q_lower:
                return sym
    return "UNKNOWN"


def extract_price_target(question: str) -> float | None:
    patterns = [
        r'\$([0-9]{1,3}(?:,[0-9]{3})+)',
        r'\$([0-9]+(?:\.[0-9]+)?)[kK]',
        r'\$([0-9]{4,})',
        r'[↑↓]\s*([0-9]{1,3}(?:,[0-9]{3})+)',
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)[kK]',
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, question)
        if match:
            raw = match.group(1).replace(",", "")
            try:
                value = float(raw)
                if 'k' in question[match.start():match.end()].lower():
                    value *= 1000
                return value
            except ValueError:
                continue
    return None
