"""
src/utils/config.py
===================
Loader konfigurasi dari file .env menggunakan python-dotenv.

Semua konfigurasi runtime dibaca dari sini.
Tidak ada hardcoded value di tempat lain.
"""

import os
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv


# Load .env dari root proyek
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env.secret")  # credentials — prioritas pertama
load_dotenv(_ROOT / ".env.local")   # settings/strategy
load_dotenv(_ROOT / ".env")         # fallback


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _get_decimal(key: str, default: str) -> Decimal:
    return Decimal(os.getenv(key, default))

def _get_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def _get_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")

def _get_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    # --- AUTHENTICATION & API KEYS ---
    PK_PRIVATE_KEY     : str = _get("PK_PRIVATE_KEY")
    API_KEY            : str = _get("CLOB_API_KEY")
    API_SECRET         : str = _get("CLOB_SECRET")
    API_PASSPHRASE     : str = _get("CLOB_PASS")
    FRED_API_KEY       : str = _get("FRED_API_KEY", "")
    ALPHA_VANTAGE_KEY  : str = _get("ALPHA_VANTAGE_KEY", "")
    TELEGRAM_BOT_TOKEN : str = _get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   : str = _get("TELEGRAM_CHAT_ID", "")

    # --- ENDPOINTS ---
    CLOB_HOST       : str = _get("CLOB_HOST", "https://clob.polymarket.com")
    GAMMA_HOST      : str = _get("GAMMA_HOST", "https://gamma-api.polymarket.com")

    # --- TARGET MARKET ---
    MARKET_ID       : str = _get("MARKET_ID")
    TOKEN_ID        : str = _get("TOKEN_ID")

    # --- STRATEGY ---
    STRATEGY_MODE   : str     = _get("STRATEGY_MODE", "mispricing")
    SPREAD_MINIMUM  : Decimal = _get_decimal("SPREAD_MINIMUM", "0.0004")
    JUMLAH_TICK     : int     = _get_int("JUMLAH_TICK", 1)
    UKURAN_ORDER    : Decimal = _get_decimal("UKURAN_ORDER", "10")
    MODE_SATU_SISI  : bool    = _get_bool("MODE_SATU_SISI", False)
    MISPRICING_THRESHOLD  : float = _get_float("MISPRICING_THRESHOLD", 0.15)
    POLITICAL_THRESHOLD   : float = _get_float("POLITICAL_THRESHOLD", 0.08)
    POLITICAL_MIN_VOLUME  : float = _get_float("POLITICAL_MIN_VOLUME", 5000.0)
    METACULUS_MATCH_SCORE : float = _get_float("METACULUS_MATCH_SCORE", 0.85)

    # --- RISK MANAGEMENT (KELLY) ---
    KELLY_MULTIPLIER       : float = _get_float("KELLY_MULTIPLIER", 0.5)
    MAX_KELLY_FRACTION     : float = _get_float("MAX_KELLY_FRACTION", 0.30)
    MIN_BET_USDC           : float = _get_float("MIN_BET_USDC", 5.0)
    MIN_WINRATE            : float = _get_float("MIN_WINRATE", 0.52)
    MIN_PROFIT_PCT         : float = _get_float("MIN_PROFIT_PCT", 0.15)
    MAX_OPEN_POSITIONS     : int   = _get_int("MAX_OPEN_POSITIONS", 5)
    MAX_CAPITAL_PER_MARKET : float = _get_float("MAX_CAPITAL_PER_MARKET", 15.0)
    TRAILING_STOP_PCT       : float = _get_float("TRAILING_STOP_PCT", 0.15)
    TIGHT_TRAILING_STOP_PCT : float = _get_float("TIGHT_TRAILING_STOP_PCT", 0.07)
    PROFIT_THRESHOLD        : float = _get_float("PROFIT_THRESHOLD", 0.85)

    # --- MARKET FILTERS ---
    MIN_MARKET_VOLUME   : float = _get_float("MIN_MARKET_VOLUME", 10_000)
    MIN_MARKET_LIQUIDITY: float = _get_float("MIN_MARKET_LIQUIDITY", 5_000)
    MAX_DAYS_TO_RESOLVE : int   = _get_int("MAX_DAYS_TO_RESOLVE", 7)
    MIN_DAYS_TO_RESOLVE : int   = _get_int("MIN_DAYS_TO_RESOLVE", 1)

    # --- BACKTEST ---
    SALDO_AWAL      : Decimal = _get_decimal("SALDO_AWAL", "1000")
    CSV_PATH        : str     = _get("CSV_PATH", "data/historical/market_log.csv")

    # --- SYSTEM ---
    POLLING_INTERVAL: int  = _get_int("POLLING_INTERVAL_DETIK", 5)
    LOG_LEVEL       : str  = _get("LOG_LEVEL", "INFO")
    DRY_RUN         : bool = _get_bool("DRY_RUN", True)


config = Config()

if not config.DRY_RUN:
    _REQUIRED = {
        "PK_PRIVATE_KEY": config.PK_PRIVATE_KEY,
        "CLOB_API_KEY":   config.API_KEY,
        "CLOB_SECRET":    config.API_SECRET,
        "CLOB_PASS":      config.API_PASSPHRASE,
    }
    _missing = [k for k, v in _REQUIRED.items() if not v]
    if _missing:
        raise EnvironmentError(
            f"Kredensial wajib tidak ditemukan di .env.secret: {', '.join(_missing)}"
        )