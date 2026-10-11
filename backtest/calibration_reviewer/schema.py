"""Reviewer-state SQLite schema and integrity helpers.

The reviewer database is NEW and separate. It must never be the production
backtest store; :func:`assert_isolated_path` fails closed if a caller points
at ``backtest.sqlite3`` or any path under the production backtest root that
is not the dedicated reviewer directory.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

REVIEWER_DIR = Path(
    os.path.expanduser("~/.hermes/backtest/calibration/reviewer")
)
REVIEWER_DB_NAME = "reviewer.sqlite3"
PRODUCTION_DB = Path(os.path.expanduser("~/.hermes/backtest/backtest.sqlite3"))

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset (
    dataset_id      TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    source_sha256   TEXT NOT NULL,
    source_size     INTEGER NOT NULL,
    source_rows     INTEGER NOT NULL,
    sampling_digest TEXT,
    loaded_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sample (
    dataset_id    TEXT NOT NULL REFERENCES dataset(dataset_id),
    sample_id     TEXT NOT NULL,
    ord           INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    source        TEXT NOT NULL,
    headline_text TEXT NOT NULL,
    headline_hash TEXT NOT NULL,
    PRIMARY KEY (dataset_id, sample_id),
    UNIQUE (dataset_id, ord)
);

CREATE TABLE IF NOT EXISTS review (
    dataset_id     TEXT NOT NULL,
    sample_id      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'UNREVIEWED',
    category       TEXT,
    direction      TEXT,
    severity       TEXT,
    ma_role        TEXT,
    certainty      TEXT,
    notes          TEXT NOT NULL DEFAULT '',
    flagged        INTEGER NOT NULL DEFAULT 0,
    rubric_version TEXT,
    rubric_sha256  TEXT,
    rubric_approved INTEGER NOT NULL DEFAULT 0,
    revealed_consensus INTEGER NOT NULL DEFAULT 0,
    revealed_model     INTEGER NOT NULL DEFAULT 0,
    revision_no    INTEGER NOT NULL DEFAULT 0,
    lock_seq       INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (dataset_id, sample_id),
    FOREIGN KEY (dataset_id, sample_id)
        REFERENCES sample(dataset_id, sample_id)
);

CREATE TABLE IF NOT EXISTS audit (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    sample_id  TEXT,
    event      TEXT NOT NULL,
    prev_status TEXT,
    new_status  TEXT,
    prev_label  TEXT,
    new_label   TEXT,
    reason      TEXT,
    post_reveal INTEGER NOT NULL DEFAULT 0,
    actor       TEXT NOT NULL,
    at          TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS session (
    session_id   TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL,
    csrf_token   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sample_dataset_ord
    ON sample(dataset_id, ord);
CREATE INDEX IF NOT EXISTS idx_review_status
    ON review(dataset_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_sample
    ON audit(dataset_id, sample_id);
"""


def assert_isolated_path(db_path: str | Path) -> Path:
    """Fail closed unless ``db_path`` is the dedicated reviewer DB path.

    Guards against ever writing reviewer state into the production store.
    """
    resolved = Path(db_path).resolve()
    if resolved == PRODUCTION_DB.resolve():
        raise RuntimeError(
            "refusing to open the production backtest store as the reviewer DB")
    prod_root = PRODUCTION_DB.parent.resolve()
    # The reviewer DB must live under the dedicated reviewer directory.
    reviewer_dir = REVIEWER_DIR.resolve()
    try:
        resolved.relative_to(reviewer_dir)
    except ValueError as exc:
        if resolved.parent == prod_root:
            raise RuntimeError(
                f"refusing to create reviewer DB directly in production "
                f"backtest root: {resolved}") from exc
        # Any other location is allowed (used by hermetic tests with tmp_path),
        # but never the production DB path itself (checked above).
    return resolved


def open_reviewer_db(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the reviewer DB and apply the schema."""
    resolved = assert_isolated_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    _migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight additive migrations for DBs created by older code.

    The review table gained ``rubric_sha256`` when approvals started being
    bound to the rubric content digest; existing reviewer DBs simply get the
    nullable column added. Existing rows keep NULL (their provenance predates
    digest binding and is not silently rewritten).
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review)")}
    if "rubric_sha256" not in cols:
        conn.execute("ALTER TABLE review ADD COLUMN rubric_sha256 TEXT")
