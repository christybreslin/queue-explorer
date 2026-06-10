"""SQLite store for daily scalar snapshots.

One row per UTC date holding every scalar metric the dashboard shows, plus
provenance (epoch / slot / capture time). The table is tiny — ~400 rows back to
Pectra, well under a few MB — so a single file with a fresh connection per call
is more than enough. Per-validator history is intentionally NOT stored here; it
is fetched on demand from the archive node when needed.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "history.db"

# Metric columns in canonical order. Provenance columns (date/epoch/slot/
# captured_at) are handled separately in the schema below. gwei values are
# stored as INTEGER; eth/ratio/day values as REAL; severity as TEXT.
METRIC_COLUMNS: list[tuple[str, str]] = [
    # network
    ("active_validators", "INTEGER"),
    ("total_stake_gwei", "INTEGER"),
    ("compounding_count", "INTEGER"),
    ("compounding_share", "REAL"),
    ("pending_consolidations", "INTEGER"),
    ("pending_consolidation_targets", "INTEGER"),
    # exit queue
    ("exit_count", "INTEGER"),
    ("exit_balance_gwei", "INTEGER"),
    ("exit_queue_depth_epochs", "INTEGER"),
    ("exit_wait_hours", "REAL"),
    ("churn_limit_gwei", "INTEGER"),
    # entry queue
    ("entry_pending_count", "INTEGER"),
    ("entry_pending_eth", "REAL"),
    ("entry_finalized_count", "INTEGER"),
    ("entry_finalized_eth", "REAL"),
    ("entry_drain_days", "REAL"),
    ("entry_severity", "TEXT"),
    # partial withdrawals
    ("partial_count", "INTEGER"),
    ("partial_total_gwei", "INTEGER"),
]

PROVENANCE_COLUMNS = ["date", "epoch", "slot", "captured_at"]
ALL_COLUMNS = PROVENANCE_COLUMNS + [name for name, _ in METRIC_COLUMNS]


def _schema() -> str:
    cols = [
        "date TEXT PRIMARY KEY",
        "epoch INTEGER NOT NULL",
        "slot INTEGER NOT NULL",
        "captured_at TEXT NOT NULL",
    ]
    cols += [f"{name} {typ}" for name, typ in METRIC_COLUMNS]
    return "CREATE TABLE IF NOT EXISTS daily_snapshot (\n  " + ",\n  ".join(cols) + "\n);"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_schema())


def upsert_snapshot(row: dict) -> None:
    """Insert or replace a day's snapshot. ``row`` must contain every column in
    ALL_COLUMNS (missing keys are stored as NULL)."""
    cols = ALL_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    values = [row.get(c) for c in cols]
    with _connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO daily_snapshot ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )


def get_range(start: str | None = None, end: str | None = None) -> list[dict]:
    """Return snapshots ordered oldest→newest, optionally bounded by inclusive
    ISO dates (YYYY-MM-DD)."""
    clauses, params = [], []
    if start:
        clauses.append("date >= ?"); params.append(start)
    if end:
        clauses.append("date <= ?"); params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM daily_snapshot{where} ORDER BY date ASC", params
        ).fetchall()
    return [dict(r) for r in rows]


def latest_date() -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM daily_snapshot").fetchone()
    return row["d"] if row and row["d"] else None


def existing_dates() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT date FROM daily_snapshot").fetchall()
    return {r["date"] for r in rows}


def count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM daily_snapshot").fetchone()["n"]
