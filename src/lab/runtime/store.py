"""SQLite persistence for lab runs, events, and idempotent commits."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional


class RunStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
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

    def append_event(self, run_id: str, seq: int, kind: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events(run_id, seq, kind, payload_json) VALUES (?, ?, ?, ?)",
            (run_id, seq, kind, json.dumps(payload)),
        )
        self._conn.commit()

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, kind, payload_json FROM events WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "seq": row["seq"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload_json"]),
                }
            )
        return out

    def get_commit(self, idempotency_key: str) -> Optional[dict[str, Any]]:
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
