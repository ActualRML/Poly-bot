"""Calibration regression guard (DB-guarded, like test_backtest_recovery).

Locks the property that makes price_zone a real probability classifier: on the
491 resolved markets in research/calibration.db, the empirical YES win-rate is
strictly increasing across the five zones at T-30. If a future threshold change
breaks monotonicity, this fails. Skipped when calibration.db isn't present.
"""
import sqlite3
from pathlib import Path

import pytest

from src.classify.price_zone import classify_price_zone

_DB = Path(__file__).resolve().parents[1] / "research" / "calibration.db"
_ZONES = ("extreme_low", "low", "uncertain", "high", "extreme_high")
_MIN_N = 10


@pytest.mark.skipif(not _DB.exists(), reason="research/calibration.db not present")
def test_zone_winrates_are_monotonic_at_t30():
    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT price_t30 AS p, outcome AS o FROM calibration_data "
            "WHERE price_t30 IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    agg = {z: [0, 0] for z in _ZONES}
    for r in rows:
        z = classify_price_zone(float(r["p"]))
        if z in agg:
            agg[z][0] += 1
            agg[z][1] += int(r["o"])

    populated = [z for z in _ZONES if agg[z][0] >= _MIN_N]
    assert populated == list(_ZONES), "every zone should be populated at T-30"

    rates = [agg[z][1] / agg[z][0] for z in _ZONES]
    assert all(a < b for a, b in zip(rates, rates[1:])), f"win-rates not monotonic: {rates}"
