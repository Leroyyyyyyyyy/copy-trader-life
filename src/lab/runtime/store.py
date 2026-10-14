"""SQLite persistence for lab runs, events, and idempotent commits."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional


class RunStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Late model results are recorded from worker threads (L6 cancel/timeout).
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              status TEXT NOT NULL,
              terminal_reason TEXT,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              run_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              kind TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              trace_id TEXT,
              t_ms REAL,
              actor TEXT,
              PRIMARY KEY (run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS commits (
              idempotency_key TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              decision_id TEXT NOT NULL,
              decision_version INTEGER NOT NULL,
              payload_hash TEXT NOT NULL,
              receipt_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
              run_id TEXT PRIMARY KEY,
              next_node TEXT NOT NULL,
              status TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fences (
              run_id TEXT PRIMARY KEY,
              reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbox (
              event_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def upsert_run(
        self,
        *,
        run_id: str,
        task_id: str,
        status: str,
        terminal_reason: Optional[str],
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            self._upsert_run(run_id, task_id, status, terminal_reason, payload)

    def _upsert_run(
        self,
        run_id: str,
        task_id: str,
        status: str,
        terminal_reason: Optional[str],
        payload: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO runs(run_id, task_id, status, terminal_reason, payload_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              status=excluded.status,
              terminal_reason=excluded.terminal_reason,
              payload_json=excluded.payload_json
            """,
            (run_id, task_id, status, terminal_reason, json.dumps(payload)),
        )
        self._conn.commit()

    def append_event(
        self,
        run_id: str,
        seq: int,
        kind: str,
        payload: dict[str, Any],
        *,
        trace_id: Optional[str] = None,
        t_ms: Optional[float] = None,
        actor: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events(run_id, seq, kind, payload_json, trace_id, t_ms, actor)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, seq, kind, json.dumps(payload), trace_id, t_ms, actor),
            )
            self._conn.commit()

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT seq, kind, payload_json, trace_id, t_ms, actor
                FROM events WHERE run_id=? ORDER BY seq
                """,
                (run_id,),
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "seq": row["seq"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload_json"]),
                    "trace_id": row["trace_id"],
                    "t_ms": row["t_ms"],
                    "actor": row["actor"],
                }
            )
        return out

    def get_commit(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT receipt_json, payload_hash FROM commits WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "receipt": json.loads(row["receipt_json"]),
            "payload_hash": row["payload_hash"],
        }

    def put_commit(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        decision_id: str,
        decision_version: int,
        payload_hash: str,
        receipt: dict[str, Any],
    ) -> None:
        with self._lock:
            self._put_commit(
                idempotency_key, run_id, decision_id, decision_version, payload_hash, receipt
            )

    def _put_commit(
        self,
        idempotency_key: str,
        run_id: str,
        decision_id: str,
        decision_version: int,
        payload_hash: str,
        receipt: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO commits(
              idempotency_key, run_id, decision_id, decision_version,
              payload_hash, receipt_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                run_id,
                decision_id,
                decision_version,
                payload_hash,
                json.dumps(receipt),
            ),
        )
        self._conn.commit()

    def put_checkpoint(self, *, run_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._put_checkpoint(run_id, payload)

    def _put_checkpoint(self, run_id: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO checkpoints(run_id, next_node, status, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              next_node=excluded.next_node,
              status=excluded.status,
              payload_json=excluded.payload_json
            """,
            (
                run_id,
                str(payload.get("next_node") or ""),
                str(payload.get("status") or ""),
                json.dumps(payload),
            ),
        )
        self._conn.commit()

    def get_checkpoint(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM checkpoints WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    # --- L6: commit fences and event inbox -------------------------------------

    def put_fence(self, *, run_id: str, reason: str) -> None:
        """After cancel/timeout, no later commit may land for this run."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO fences(run_id, reason) VALUES (?, ?)",
                (run_id, reason),
            )
            self._conn.commit()

    def get_fence(self, run_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT reason FROM fences WHERE run_id=?", (run_id,)
            ).fetchone()
        return None if row is None else str(row["reason"])

    def claim_event(
        self, *, event_id: str, run_id: str, payload_hash: str
    ) -> tuple[bool, str, str]:
        """Insert-if-absent. Returns (claimed, owning_run_id, stored_payload_hash)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO inbox(event_id, run_id, payload_hash) VALUES (?, ?, ?)",
                (event_id, run_id, payload_hash),
            )
            self._conn.commit()
            if cur.rowcount == 1:
                return True, run_id, payload_hash
            row = self._conn.execute(
                "SELECT run_id, payload_hash FROM inbox WHERE event_id=?", (event_id,)
            ).fetchone()
        return False, str(row["run_id"]), str(row["payload_hash"])
