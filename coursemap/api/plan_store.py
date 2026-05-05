"""
Persistent plan store backed by SQLite.

Plans are stored keyed by a 16-char hex plan_id (SHA-256 of the request params).
The store is a single SQLite file at $COURSEMAP_DB_PATH (default: data/plans.db).
The db directory is created automatically on first use.

This replaces the in-memory OrderedDict from the prototype. Plans survive
server restarts, making shared links permanently deterministic: the same
?pid=... always returns the same plan.

Public API:
    plan_store.get(plan_id)     -> dict | None
    plan_store.put(plan_id, d)  -> None
    plan_store.count()          -> int
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(os.environ.get("COURSEMAP_DB_PATH", "data/plans.db"))
_MAX_PLANS = 10_000   # hard cap; oldest are purged when exceeded


class _PlanStore:
    """Thread-safe SQLite-backed plan cache."""

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads while writing
        conn.execute("PRAGMA synchronous=NORMAL") # faster writes, safe enough for cache
        return conn

    def _init_db(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id    TEXT PRIMARY KEY,
                    params_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    hit_count   INTEGER NOT NULL DEFAULT 0,
                    last_hit    TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_plans_created
                ON plans (created_at)
            """)
            conn.commit()
        logger.info("Plan store initialised at %s", self._path)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, plan_id: str) -> dict | None:
        """Return the stored plan dict, or None if not found."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                return None
            # Update hit stats (best-effort - don't fail a read over this)
            try:
                conn.execute(
                    "UPDATE plans SET hit_count = hit_count + 1, last_hit = datetime('now') WHERE plan_id = ?",
                    (plan_id,),
                )
                conn.commit()
            except Exception:
                pass
            return json.loads(row[0])

    def put(self, plan_id: str, params: dict, result: dict) -> None:
        """Insert or replace a plan. Prunes oldest plans if over the cap."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO plans (plan_id, params_json, result_json, created_at, hit_count)
                VALUES (?, ?, ?, datetime('now'), 0)
                """,
                (plan_id, json.dumps(params, default=str), json.dumps(result, default=str)),
            )
            conn.commit()
            # Prune oldest if we're over the cap
            count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
            if count > _MAX_PLANS:
                excess = count - _MAX_PLANS
                conn.execute(
                    "DELETE FROM plans WHERE plan_id IN "
                    "(SELECT plan_id FROM plans ORDER BY created_at ASC LIMIT ?)",
                    (excess,),
                )
                conn.commit()
                logger.debug("Pruned %d old plans from store", excess)

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]

    def stats(self) -> dict:
        """Return store statistics for the /api endpoint."""
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
            oldest = conn.execute(
                "SELECT created_at FROM plans ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            newest = conn.execute(
                "SELECT created_at FROM plans ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            top = conn.execute(
                "SELECT plan_id, hit_count FROM plans ORDER BY hit_count DESC LIMIT 5"
            ).fetchall()
        return {
            "total_plans": total,
            "oldest_plan": oldest[0] if oldest else None,
            "newest_plan": newest[0] if newest else None,
            "top_plans":   [{"plan_id": r[0], "hits": r[1]} for r in top],
            "db_path":     str(self._path),
        }


# Module-level singleton - created once on import
plan_store = _PlanStore()
