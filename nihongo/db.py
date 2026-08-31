"""SQLite persistence. Every CLI invocation opens a fresh connection, reads
what it needs, writes back, and closes — no long-running session/server, so
state is always in sync no matter which device you run `nihongo` from.
"""

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import curriculum, srs

SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    unit_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS srs_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    prompt TEXT NOT NULL,
    answer TEXT NOT NULL,
    meaning TEXT,
    ease REAL NOT NULL DEFAULT 2.5,
    interval_days REAL NOT NULL DEFAULT 0,
    reps INTEGER NOT NULL DEFAULT 0,
    due_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(item_type, prompt)
);

CREATE TABLE IF NOT EXISTS kana_batches (
    batch_id TEXT PRIMARY KEY,
    order_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'locked',
    drill_cursor INTEGER NOT NULL DEFAULT 0,
    quiz_cursor INTEGER NOT NULL DEFAULT 0,
    reading_cursor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kana_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    kana TEXT NOT NULL,
    phase TEXT NOT NULL,
    correct INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id INTEGER
);

CREATE TABLE IF NOT EXISTS kana_cursors (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    unit_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    summary TEXT
);
"""


def db_path() -> Path:
    override = os.environ.get("NIHONGO_DB_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".nihongo" / "nihongo.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    _ensure_progress_row(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Small ad-hoc migrations for columns added after a table already
    existed — CREATE TABLE IF NOT EXISTS won't add new columns on its own."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(kana_attempts)")}
    if "session_id" not in cols:
        conn.execute("ALTER TABLE kana_attempts ADD COLUMN session_id INTEGER")
        conn.commit()


def _ensure_progress_row(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT 1 FROM progress WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO progress (id, unit_id, status, updated_at) VALUES (1, ?, 'in_progress', ?)",
            (curriculum.first_unit().id, _now()),
        )
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_progress(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM progress WHERE id = 1").fetchone()


def complete_current_unit(conn: sqlite3.Connection) -> str | None:
    """Advance to the next unit. Returns the new unit id, or None if the
    learner just finished the last unit in the curriculum."""
    current = get_progress(conn)
    nxt = curriculum.next_unit(current["unit_id"])
    if nxt is None:
        conn.execute(
            "UPDATE progress SET status = 'completed', updated_at = ? WHERE id = 1",
            (_now(),),
        )
        conn.commit()
        return None
    conn.execute(
        "UPDATE progress SET unit_id = ?, status = 'in_progress', updated_at = ? WHERE id = 1",
        (nxt.id, _now()),
    )
    conn.commit()
    return nxt.id


def upsert_srs_item(
    conn: sqlite3.Connection,
    item_type: str,
    prompt: str,
    answer: str,
    meaning: str | None = None,
) -> None:
    existing = conn.execute(
        "SELECT id FROM srs_items WHERE item_type = ? AND prompt = ?",
        (item_type, prompt),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """INSERT INTO srs_items
           (item_type, prompt, answer, meaning, ease, interval_days, reps, due_date, created_at)
           VALUES (?, ?, ?, ?, 2.5, 0, 0, ?, ?)""",
        (item_type, prompt, answer, meaning, date.today().isoformat(), _now()),
    )
    conn.commit()


def get_due_srs_items(
    conn: sqlite3.Connection, item_type: str | None = None, limit: int = 20
) -> list[sqlite3.Row]:
    today = date.today().isoformat()
    if item_type:
        return conn.execute(
            "SELECT * FROM srs_items WHERE item_type = ? AND due_date <= ? ORDER BY due_date LIMIT ?",
            (item_type, today, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM srs_items WHERE due_date <= ? ORDER BY due_date LIMIT ?",
        (today, limit),
    ).fetchall()


def review_srs_item(conn: sqlite3.Connection, item_id: int, quality: int) -> None:
    row = conn.execute("SELECT * FROM srs_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return
    state = srs.SrsState(ease=row["ease"], interval_days=row["interval_days"], reps=row["reps"])
    new_state, due = srs.review(state, quality, date.today())
    conn.execute(
        "UPDATE srs_items SET ease = ?, interval_days = ?, reps = ?, due_date = ? WHERE id = ?",
        (new_state.ease, new_state.interval_days, new_state.reps, due.isoformat(), item_id),
    )
    conn.commit()


def srs_stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM srs_items").fetchone()[0]
    due = conn.execute(
        "SELECT COUNT(*) FROM srs_items WHERE reps > 0 AND due_date <= ?",
        (date.today().isoformat(),),
    ).fetchone()[0]
    by_type = conn.execute(
        "SELECT item_type, COUNT(*) as n FROM srs_items GROUP BY item_type"
    ).fetchall()
    return {"total": total, "due": due, "by_type": {r["item_type"]: r["n"] for r in by_type}}


def all_srs_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every vocab/grammar item, soonest due first, for the detailed progress report."""
    return conn.execute("SELECT * FROM srs_items ORDER BY due_date").fetchall()


def start_session(conn: sqlite3.Connection, kind: str, unit_id: str) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (kind, unit_id, started_at) VALUES (?, ?, ?)",
        (kind, unit_id, _now()),
    )
    conn.commit()
    return cur.lastrowid


def end_session(conn: sqlite3.Connection, session_id: int, summary: str | None = None) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ?",
        (_now(), summary, session_id),
    )
    conn.commit()


def recent_sessions(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def session_count(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT kind, COUNT(*) as n FROM sessions GROUP BY kind").fetchall()
    by_kind = {r["kind"]: r["n"] for r in rows}
    return {"total": sum(by_kind.values()), "by_kind": by_kind}


def total_practice_seconds(conn: sqlite3.Connection) -> float:
    rows = conn.execute(
        "SELECT started_at, ended_at FROM sessions WHERE ended_at IS NOT NULL"
    ).fetchall()
    total = 0.0
    for r in rows:
        start = datetime.fromisoformat(r["started_at"])
        end = datetime.fromisoformat(r["ended_at"])
        total += (end - start).total_seconds()
    return total


def session_kana_stats(conn: sqlite3.Connection) -> dict[int, tuple[int, int]]:
    """Maps session_id -> (attempts, correct) over recognition-phase kana
    attempts, for scoring each row in the session-history report."""
    rows = conn.execute(
        """SELECT session_id, COUNT(*) AS attempts, SUM(correct) AS correct
           FROM kana_attempts
           WHERE session_id IS NOT NULL AND phase = 'recognition'
           GROUP BY session_id"""
    ).fetchall()
    return {r["session_id"]: (r["attempts"], r["correct"]) for r in rows}


# --- kana engine persistence -------------------------------------------------

def init_kana_batches(conn: sqlite3.Connection, batch_ids: list[str]) -> None:
    existing = {r["batch_id"] for r in conn.execute("SELECT batch_id FROM kana_batches")}
    for i, batch_id in enumerate(batch_ids):
        if batch_id in existing:
            continue
        status = "unlocked" if i == 0 else "locked"
        conn.execute(
            "INSERT INTO kana_batches (batch_id, order_index, status) VALUES (?, ?, ?)",
            (batch_id, i, status),
        )
    conn.commit()


def get_kana_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM kana_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def all_kana_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM kana_batches ORDER BY order_index").fetchall()


def get_active_kana_batch_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT batch_id FROM kana_batches WHERE status IN ('unlocked','introduced') "
        "ORDER BY order_index LIMIT 1"
    ).fetchone()
    return row["batch_id"] if row else None


def set_kana_batch_status(conn: sqlite3.Connection, batch_id: str, status: str) -> None:
    conn.execute(
        "UPDATE kana_batches SET status = ? WHERE batch_id = ?", (status, batch_id)
    )
    conn.commit()


def bump_kana_cursor_column(conn: sqlite3.Connection, batch_id: str, column: str, by: int) -> None:
    assert column in ("drill_cursor", "quiz_cursor", "reading_cursor")
    conn.execute(
        f"UPDATE kana_batches SET {column} = {column} + ? WHERE batch_id = ?",
        (by, batch_id),
    )
    conn.commit()


def record_kana_attempt(
    conn: sqlite3.Connection, batch_id: str, kana: str, phase: str, correct: bool, session_id: int | None = None
) -> None:
    conn.execute(
        "INSERT INTO kana_attempts (batch_id, kana, phase, correct, local_date, created_at, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, kana, phase, int(correct), date.today().isoformat(), _now(), session_id),
    )
    conn.commit()


def kana_attempts_for(
    conn: sqlite3.Connection, batch_id: str, kana: str, phase: str = "recognition"
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT correct, local_date FROM kana_attempts "
        "WHERE batch_id = ? AND kana = ? AND phase = ? ORDER BY id DESC",
        (batch_id, kana, phase),
    ).fetchall()


def kana_attempts_for_batch(
    conn: sqlite3.Connection, batch_id: str, phase: str = "recognition", limit: int = 20
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT correct, local_date FROM kana_attempts "
        "WHERE batch_id = ? AND phase = ? ORDER BY id DESC LIMIT ?",
        (batch_id, phase, limit),
    ).fetchall()


def distinct_kana_attempt_sessions(conn: sqlite3.Connection, batch_id: str, phase: str = "recognition") -> list[int]:
    """Distinct practice occasions (rounds) a batch has recognition attempts
    in — this is the unit mastery pacing is measured against, not calendar
    days, so 2-3 rows/day is achievable by doing a couple of solid rounds
    per row rather than waiting for real calendar days to pass."""
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM kana_attempts "
        "WHERE batch_id = ? AND phase = ? AND session_id IS NOT NULL",
        (batch_id, phase),
    ).fetchall()
    return [r["session_id"] for r in rows]


def attempts_in_session(conn: sqlite3.Connection, batch_id: str, session_id: int, phase: str = "recognition") -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM kana_attempts WHERE batch_id = ? AND phase = ? AND session_id = ?",
        (batch_id, phase, session_id),
    ).fetchone()[0]


def kana_char_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Lifetime attempts/correct/last-practiced-date per character, over
    recognition-phase attempts, for the detailed progress report."""
    return conn.execute(
        """SELECT batch_id, kana, COUNT(*) AS attempts, SUM(correct) AS correct,
                  MAX(local_date) AS last_date
           FROM kana_attempts
           WHERE phase = 'recognition'
           GROUP BY batch_id, kana"""
    ).fetchall()


def total_kana_accuracy(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(correct), 0) AS c FROM kana_attempts WHERE phase = 'recognition'"
    ).fetchone()
    return row["n"], row["c"]


def get_kana_cursor(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT position FROM kana_cursors WHERE name = ?", (name,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO kana_cursors (name, position) VALUES (?, 0)", (name,))
        conn.commit()
        return 0
    return row["position"]


def bump_kana_cursor(conn: sqlite3.Connection, name: str, by: int) -> None:
    get_kana_cursor(conn, name)  # ensure row exists
    conn.execute("UPDATE kana_cursors SET position = position + ? WHERE name = ?", (by, name))
    conn.commit()
