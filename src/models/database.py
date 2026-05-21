
import sqlite3
import logging
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "bot_database.db"

@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    question        TEXT NOT NULL,
    outcome         TEXT NOT NULL,          -- "Yes" atau "No"
    entry_price     TEXT NOT NULL,          -- Decimal as string
    current_price   TEXT NOT NULL,
    highest_price   TEXT NOT NULL,
    shares          TEXT NOT NULL,
    capital_at_risk TEXT NOT NULL,
    resolve_date    TEXT NOT NULL,          -- ISO format
    entry_time      TEXT NOT NULL,
    status          TEXT DEFAULT 'open',    -- open | closed
    exit_price      TEXT,
    exit_time       TEXT,
    pnl_usdc        TEXT,
    exit_reason     TEXT,
    gap_pct         TEXT,                   -- mispricing gap saat entry
    kelly_fraction  TEXT,                   -- kelly fraction yang dipakai
    strategy_mode   TEXT,                   -- mispricing | market_making
    sym_m5m         REAL,                   -- symbol's own 5m momentum at entry
    sym_m15m        REAL,                   -- symbol's own 15m momentum at entry
    sym_m30m        REAL,                   -- symbol's own 30m momentum at entry
    vol_ratio       REAL,                   -- volume ratio recent vs baseline
    btc_m15m        REAL,                   -- BTC 15m momentum at entry (macro regime)
    scout_score     INTEGER,                -- scout composite sub-score (renamed from regime_score)
    mtf_aligned     INTEGER,                -- 1 if 5m/15m/30m aligned, else 0
    predicted_prob  REAL,                   -- heuristic winrate model output at entry
    signal_breakdown TEXT,                  -- JSON per-signal breakdown from calculate_winrate
    UNIQUE(condition_id, outcome)
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    question        TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    action          TEXT NOT NULL,          -- buy | sell | exit
    price           TEXT NOT NULL,
    shares          TEXT NOT NULL,
    usdc_amount     TEXT NOT NULL,
    strategy_mode   TEXT,                   -- mispricing | market_making
    gap_pct         TEXT,                   -- mispricing gap saat entry
    kelly_fraction  TEXT,                   -- kelly fraction yang dipakai
    timestamp       TEXT NOT NULL,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_condition ON positions(condition_id);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);

CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    question        TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    predicted_prob  TEXT NOT NULL,   -- probabilitas dari model kita
    market_price    TEXT NOT NULL,   -- harga pasar saat prediksi
    gap_pct         TEXT NOT NULL,   -- mispricing gap
    prediction_date TEXT NOT NULL,   -- kapan prediksi dibuat
    resolve_date    TEXT,            -- kapan market resolve
    actual_outcome  INTEGER,         -- NULL=pending, 1=menang, 0=kalah
    resolve_price   TEXT,            -- harga settlement aktual
    resolved_at     TEXT,            -- kapan dicatat resolved
    UNIQUE(condition_id, outcome)
);

CREATE INDEX IF NOT EXISTS idx_predictions_resolved ON predictions(actual_outcome);
"""

def init_db():

    with get_conn() as conn:
        conn.executescript(SCHEMA)

        _migrate(conn)
    logger.info(f"Database ready: {DB_PATH}")

def _migrate(conn):

    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()
    }
    new_columns = {
        "gap_pct":        "ALTER TABLE positions ADD COLUMN gap_pct TEXT",
        "kelly_fraction": "ALTER TABLE positions ADD COLUMN kelly_fraction TEXT",
        "strategy_mode":  "ALTER TABLE positions ADD COLUMN strategy_mode TEXT",
        "token_id":       "ALTER TABLE positions ADD COLUMN token_id TEXT",
        "sym_m5m":        "ALTER TABLE positions ADD COLUMN sym_m5m REAL",
        "sym_m15m":       "ALTER TABLE positions ADD COLUMN sym_m15m REAL",
        "sym_m30m":       "ALTER TABLE positions ADD COLUMN sym_m30m REAL",
        "vol_ratio":      "ALTER TABLE positions ADD COLUMN vol_ratio REAL",
        "btc_m15m":       "ALTER TABLE positions ADD COLUMN btc_m15m REAL",
        "scout_score":    "ALTER TABLE positions ADD COLUMN scout_score INTEGER",
        "mtf_aligned":    "ALTER TABLE positions ADD COLUMN mtf_aligned INTEGER",
        "predicted_prob": "ALTER TABLE positions ADD COLUMN predicted_prob REAL",
        "signal_breakdown": "ALTER TABLE positions ADD COLUMN signal_breakdown TEXT",
    }
    if "regime_score" in existing and "scout_score" not in existing:
        try:
            sqlite_ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
            if sqlite_ver >= (3, 25, 0):
                conn.execute("ALTER TABLE positions RENAME COLUMN regime_score TO scout_score")
                logger.info("[DB MIGRATE] regime_score → scout_score (RENAME COLUMN)")
            else:
                conn.execute("ALTER TABLE positions ADD COLUMN scout_score INTEGER")
                conn.execute("UPDATE positions SET scout_score = regime_score")
                logger.info("[DB MIGRATE] regime_score → scout_score (ADD+COPY fallback)")
            existing.add("scout_score")
            existing.discard("regime_score")
        except Exception as e:
            logger.error(f"[DB MIGRATE] regime_score → scout_score rename failed: {e}")
            raise
    for col, sql in new_columns.items():
        if col not in existing:
            try:
                conn.execute(sql)
                logger.info(f"[DB MIGRATE] Kolom '{col}' ditambahkan ke positions")
            except Exception as e:
                logger.error(f"[DB MIGRATE] Gagal tambah kolom '{col}': {e}")
                raise

def save_position(pos: dict) -> int:

    sql = """
        INSERT INTO positions
            (condition_id, question, outcome, entry_price, current_price,
             highest_price, shares, capital_at_risk, resolve_date, entry_time,
             gap_pct, kelly_fraction, strategy_mode, token_id,
             sym_m5m, sym_m15m, sym_m30m, vol_ratio, btc_m15m, scout_score, mtf_aligned,
             predicted_prob, signal_breakdown)
        VALUES
            (:condition_id, :question, :outcome, :entry_price, :current_price,
             :highest_price, :shares, :capital_at_risk, :resolve_date, :entry_time,
             :gap_pct, :kelly_fraction, :strategy_mode, :token_id,
             :sym_m5m, :sym_m15m, :sym_m30m, :vol_ratio, :btc_m15m, :scout_score, :mtf_aligned,
             :predicted_prob, :signal_breakdown)
        ON CONFLICT(condition_id, outcome) DO UPDATE SET
            status          = 'open',
            question        = excluded.question,
            entry_price     = excluded.entry_price,
            current_price   = excluded.current_price,
            highest_price   = excluded.highest_price,
            shares          = excluded.shares,
            capital_at_risk = excluded.capital_at_risk,
            resolve_date    = excluded.resolve_date,
            entry_time      = excluded.entry_time,
            gap_pct         = excluded.gap_pct,
            kelly_fraction  = excluded.kelly_fraction,
            strategy_mode   = excluded.strategy_mode,
            token_id        = excluded.token_id,
            sym_m5m         = excluded.sym_m5m,
            sym_m15m        = excluded.sym_m15m,
            sym_m30m        = excluded.sym_m30m,
            vol_ratio       = excluded.vol_ratio,
            btc_m15m        = excluded.btc_m15m,
            scout_score    = excluded.scout_score,
            mtf_aligned     = excluded.mtf_aligned,
            predicted_prob  = excluded.predicted_prob,
            signal_breakdown = excluded.signal_breakdown,
            exit_price      = NULL,
            exit_time       = NULL,
            pnl_usdc        = NULL,
            exit_reason     = NULL
    """

    normalized = {k: str(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
    if isinstance(normalized.get("resolve_date"), datetime):
        normalized["resolve_date"] = normalized["resolve_date"].isoformat()
    if isinstance(normalized.get("entry_time"), datetime):
        normalized["entry_time"] = normalized["entry_time"].isoformat()

    normalized.setdefault("gap_pct", None)
    normalized.setdefault("kelly_fraction", None)
    normalized.setdefault("strategy_mode", None)
    normalized.setdefault("token_id", None)
    normalized.setdefault("sym_m5m", None)
    normalized.setdefault("sym_m15m", None)
    normalized.setdefault("sym_m30m", None)
    normalized.setdefault("vol_ratio", None)
    normalized.setdefault("btc_m15m", None)
    normalized.setdefault("scout_score", None)
    normalized.setdefault("mtf_aligned", None)
    normalized.setdefault("predicted_prob", None)
    normalized.setdefault("signal_breakdown", None)

    with get_conn() as conn:
        cur = conn.execute(sql, normalized)
        return cur.lastrowid

def update_position_price(condition_id: str, outcome: str, current_price: Decimal):

    sql = """
        UPDATE positions SET
            current_price = :current_price,
            highest_price = MAX(highest_price, :current_price)
        WHERE condition_id = :condition_id AND outcome = :outcome AND status = 'open'
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "condition_id": condition_id,
            "outcome": outcome,
            "current_price": str(current_price),
        })

def update_position_token_id(condition_id: str, outcome: str, token_id: str):

    sql = """
        UPDATE positions SET token_id = :token_id
        WHERE condition_id = :condition_id AND outcome = :outcome AND status = 'open'
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "condition_id": condition_id,
            "outcome": outcome,
            "token_id": token_id,
        })

def close_position(
    condition_id: str,
    outcome: str,
    exit_price: Decimal,
    exit_reason: str,
    pnl_usdc: Decimal,
):

    sql = """
        UPDATE positions SET
            status     = 'closed',
            exit_price = :exit_price,
            exit_time  = :exit_time,
            pnl_usdc   = :pnl_usdc,
            exit_reason= :exit_reason
        WHERE condition_id = :condition_id AND outcome = :outcome AND status = 'open'
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "condition_id": condition_id,
            "outcome": outcome,
            "exit_price": str(exit_price),
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "pnl_usdc": str(pnl_usdc),
            "exit_reason": exit_reason,
        })

def get_open_positions() -> list[dict]:

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time"
        ).fetchall()
        return [dict(r) for r in rows]

def get_position(condition_id: str, outcome: str) -> Optional[dict]:

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE condition_id = ? AND outcome = ? AND status = 'open'",
            (condition_id, outcome)
        ).fetchone()
        return dict(row) if row else None

def get_position_by_market(condition_id: str) -> Optional[dict]:

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE condition_id = ? AND status = 'open' LIMIT 1",
            (condition_id,)
        ).fetchone()
        return dict(row) if row else None

def count_open_positions() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'open'"
        ).fetchone()[0]

def count_open_by_direction(outcome: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'open' AND outcome = ?",
            (outcome,)
        ).fetchone()[0]

def count_open_by_resolve_slot(resolve_dt: datetime, window_minutes: int = 30) -> int:
    from datetime import timedelta
    lo = (resolve_dt - timedelta(minutes=window_minutes)).isoformat()
    hi = (resolve_dt + timedelta(minutes=window_minutes)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'open' AND resolve_date BETWEEN ? AND ?",
            (lo, hi)
        ).fetchone()[0]

def log_trade(trade: dict):

    sql = """
        INSERT INTO trades
            (condition_id, question, outcome, action, price, shares,
             usdc_amount, strategy_mode, gap_pct, kelly_fraction, timestamp, notes)
        VALUES
            (:condition_id, :question, :outcome, :action, :price, :shares,
             :usdc_amount, :strategy_mode, :gap_pct, :kelly_fraction, :timestamp, :notes)
    """
    normalized = {k: str(v) if isinstance(v, Decimal) else v for k, v in trade.items()}
    normalized.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    normalized.setdefault("strategy_mode", None)
    normalized.setdefault("gap_pct", None)
    normalized.setdefault("kelly_fraction", None)
    normalized.setdefault("notes", None)

    with get_conn() as conn:
        conn.execute(sql, normalized)

def get_trade_history(limit: int = 50) -> list[dict]:

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_stats() -> dict:

    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                                        AS total_trades,
                SUM(CAST(usdc_amount AS REAL))                  AS total_pnl,
                SUM(CASE WHEN CAST(usdc_amount AS REAL) > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN CAST(usdc_amount AS REAL) <= 0 THEN 1 ELSE 0 END) AS losses,
                AVG(CAST(usdc_amount AS REAL))                  AS avg_pnl
            FROM trades
            WHERE action IN ('exit', 'sell')
              AND (notes IS NULL OR notes != 'force_close_no_price')
        """).fetchone()
        d = dict(row)
        total = d["total_trades"] or 0
        wins = d["wins"] or 0
        d["winrate"] = round(wins / total * 100, 1) if total > 0 else 0
        return d

def log_prediction(data: dict):

    sql = """
        INSERT INTO predictions
            (condition_id, question, outcome, predicted_prob,
             market_price, gap_pct, prediction_date, resolve_date)
        VALUES
            (:condition_id, :question, :outcome, :predicted_prob,
             :market_price, :gap_pct, :prediction_date, :resolve_date)
        ON CONFLICT(condition_id, outcome) DO NOTHING
    """
    normalized = {k: str(v) if isinstance(v, Decimal) else v for k, v in data.items()}
    normalized.setdefault("prediction_date", datetime.now(timezone.utc).isoformat())
    normalized.setdefault("resolve_date", None)
    with get_conn() as conn:
        conn.execute(sql, normalized)

def resolve_prediction(condition_id: str, outcome: str, won: bool, resolve_price: float):

    sql = """
        UPDATE predictions SET
            actual_outcome = :actual_outcome,
            resolve_price  = :resolve_price,
            resolved_at    = :resolved_at
        WHERE condition_id = :condition_id AND outcome = :outcome
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "condition_id":   condition_id,
            "outcome":        outcome,
            "actual_outcome": 1 if won else 0,
            "resolve_price":  str(resolve_price),
            "resolved_at":    datetime.now(timezone.utc).isoformat(),
        })

def get_recent_closed_pnls(limit: int = 5) -> list[dict]:

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pnl_usdc FROM positions
               WHERE status = 'closed' AND pnl_usdc IS NOT NULL
                 AND (exit_reason IS NULL OR exit_reason != 'force_close_no_price')
               ORDER BY exit_time DESC LIMIT ?""",
            (limit,)
        ).fetchall()

    return [{"pnl": float(r["pnl_usdc"])} for r in reversed(rows)]


def get_recent_closed_hourly(limit: int = 10) -> list[dict]:
    """Recent closed hourly positions (live + dry-run) with question + pnl.
    Used by per-symbol blacklist to detect loss streaks for a given asset."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT question, pnl_usdc FROM positions
               WHERE status = 'closed' AND pnl_usdc IS NOT NULL
                 AND strategy_mode LIKE 'updown_hourly%'
                 AND (exit_reason IS NULL OR exit_reason != 'force_close_no_price')
               ORDER BY exit_time DESC LIMIT ?""",
            (limit,)
        ).fetchall()

    return [{"question": r["question"], "pnl": float(r["pnl_usdc"])} for r in rows]

def get_accuracy_report() -> dict:

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT predicted_prob, market_price, gap_pct, actual_outcome
            FROM predictions
            WHERE actual_outcome IS NOT NULL
            ORDER BY prediction_date
        """).fetchall()

    if not rows:
        return {"total": 0, "message": "Belum ada prediksi yang resolved"}

    total      = len(rows)
    wins       = sum(1 for r in rows if r["actual_outcome"] == 1)
    avg_pred   = sum(float(r["predicted_prob"]) for r in rows) / total
    avg_market = sum(float(r["market_price"]) for r in rows) / total
    winrate    = wins / total

    buckets: dict[int, list] = {}
    for r in rows:
        p      = float(r["predicted_prob"])
        bucket = int(p * 10) * 10
        buckets.setdefault(bucket, []).append(r["actual_outcome"])

    calibration = {
        f"{k}-{k+10}%": {
            "predicted": f"{(k+5):.0f}%",
            "actual":    f"{sum(v)/len(v)*100:.0f}%",
            "n":         len(v),
        }
        for k, v in sorted(buckets.items())
        if len(v) >= 2
    }

    mae = sum(abs(float(r["predicted_prob"]) - r["actual_outcome"]) for r in rows) / total

    return {
        "total":         total,
        "wins":          wins,
        "losses":        total - wins,
        "winrate":       round(winrate * 100, 1),
        "avg_predicted": round(avg_pred * 100, 1),
        "avg_market":    round(avg_market * 100, 1),
        "mae":           round(mae, 3),
        "calibration":   calibration,
        "verdict":       _calibration_verdict(winrate, avg_pred, mae),
    }

def _calibration_verdict(winrate: float, avg_pred: float, mae: float) -> str:
    if mae < 0.10:
        return "Model sangat akurat (MAE < 10%)"
    if mae < 0.20:
        return "Model cukup akurat (MAE < 20%) — layak live"
    if winrate > avg_pred + 0.10:
        return "Model under-confident — bisa naikkan bet size"
    if winrate < avg_pred - 0.10:
        return "Model over-confident — turunkan bet size atau tunggu lebih banyak data"
    return f"Model perlu lebih banyak data (MAE={mae:.2f})"
