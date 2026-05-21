import json
from datetime import datetime, timezone

import src.models.database as db


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_schema_has_diagnostic_columns(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with db.get_conn() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    assert {"scout_score", "predicted_prob", "signal_breakdown"} <= cols


def test_open_position_persists_score_prob_breakdown(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    from src.execute.position import PositionManager
    from decimal import Decimal

    mgr = PositionManager()
    breakdown = json.dumps({"mom_15m_strong_aligned": True, "score": 4})
    ok = mgr.open_position(
        condition_id="cond-1",
        question="BTC Up or Down - test",
        outcome="Up",
        entry_price=Decimal("0.45"),
        shares=Decimal("10"),
        capital_at_risk=Decimal("4.5"),
        resolve_date=datetime.now(timezone.utc),
        scout_score=4,
        predicted_prob=0.6,
        signal_breakdown=breakdown,
    )
    assert ok

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT scout_score, predicted_prob, signal_breakdown FROM positions "
            "WHERE condition_id='cond-1'"
        ).fetchone()
    assert row["scout_score"] == 4
    assert row["predicted_prob"] == 0.6
    assert json.loads(row["signal_breakdown"])["score"] == 4
