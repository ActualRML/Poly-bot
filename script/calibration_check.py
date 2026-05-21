"""
Quick calibration check: compare predicted_prob to actual WR.
Run after sample size reaches 50+ trades.
"""
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "bot_database.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT predicted_prob, pnl_usdc
        FROM positions
        WHERE status='closed' AND predicted_prob IS NOT NULL
    """).fetchall()

    if len(rows) < 10:
        print(f"Only {len(rows)} trades with predicted_prob - need 50+ for meaningful calibration")
        return

    buckets = defaultdict(lambda: {'count': 0, 'wins': 0})
    for prob, pnl in rows:
        bucket = round(float(prob) * 10) / 10
        buckets[bucket]['count'] += 1
        if float(pnl) > 0:
            buckets[bucket]['wins'] += 1

    print(f"Calibration check ({len(rows)} trades)")
    print(f"{'predicted':<12} {'actual_WR':<12} {'count':<8} {'verdict'}")
    print("-" * 50)
    for bucket in sorted(buckets.keys(), reverse=True):
        data = buckets[bucket]
        actual_wr = data['wins'] / data['count']
        diff = actual_wr - bucket
        verdict = "OK calibrated" if abs(diff) < 0.05 else (
            "under-confident" if diff > 0 else "OVER-confident")
        print(f"{bucket:<12.2f} {actual_wr:<12.2%} {data['count']:<8} {verdict}")


if __name__ == "__main__":
    main()
