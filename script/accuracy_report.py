"""
script/accuracy_report.py
==========================
Laporan akurasi model — seberapa akurat prediksi probabilitas kita vs outcome nyata.

Jalankan dengan:
    python -m script.accuracy_report
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models.database import get_accuracy_report, get_conn


def main():
    print("=" * 60)
    print("  MODEL ACCURACY REPORT")
    print("=" * 60)

    report = get_accuracy_report()

    if report["total"] == 0:
        print(f"\n  {report.get('message', 'Belum ada data.')}")
        print("\n  Tunggu minimal 1 posisi resolved untuk melihat laporan.")
        _show_pending()
        return

    print(f"\n📊 RINGKASAN ({report['total']} prediksi resolved)")
    print(f"   Menang    : {report['wins']}")
    print(f"   Kalah     : {report['losses']}")
    print(f"   Win Rate  : {report['winrate']}%")
    print(f"   Avg Prediksi Model : {report['avg_predicted']}%")
    print(f"   Avg Harga Pasar    : {report['avg_market']}%")
    print(f"   MAE (error rata2)  : {report['mae']:.3f}")

    print(f"\n🎯 VERDICT: {report['verdict']}")

    if report.get("calibration"):
        print("\n📈 KALIBRASI (predicted vs actual per bucket):")
        for bucket, data in report["calibration"].items():
            bar = "█" * data["n"]
            print(f"   {bucket:10s} → predicted {data['predicted']:4s} | actual {data['actual']:4s} | n={data['n']} {bar}")

    _show_pending()

    print("\n" + "=" * 60)


def _show_pending():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT question, outcome, predicted_prob, market_price, gap_pct,
                   prediction_date, resolve_date
            FROM predictions
            WHERE actual_outcome IS NULL
            ORDER BY resolve_date
        """).fetchall()

    if not rows:
        return

    print(f"\n⏳ PREDIKSI PENDING ({len(rows)} posisi belum resolved):")
    for r in rows:
        pred  = float(r["predicted_prob"]) * 100
        mkt   = float(r["market_price"]) * 100
        gap   = float(r["gap_pct"])
        rdate = (r["resolve_date"] or "?")[:10]
        print(
            f"   {r['question'][:45]:<45} | {r['outcome']:3} | "
            f"pred={pred:.0f}% mkt={mkt:.0f}% gap={gap:.1f}% | resolve={rdate}"
        )


if __name__ == "__main__":
    main()
