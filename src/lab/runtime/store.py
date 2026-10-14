"""SQLite persistence for lab runs, events, and idempotent commits."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional


class StaleAccountError(Exception):
    """The persisted account moved since the caller read it (optimistic lock)."""


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
            CREATE TABLE IF NOT EXISTS accounts (
              account_id TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
              card_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_responses (
              response_id TEXT PRIMARY KEY,
              card_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              result_json TEXT NOT NULL
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
        account_id: Optional[str] = None,
        account: Optional[dict[str, Any]] = None,
        account_expected_version: Optional[int] = None,
    ) -> None:
        """Insert the receipt; if an account is given, save it in the same transaction.

        `account_expected_version` is a compare-and-swap: another process may have
        changed the account after this caller loaded it, and that change must not be
        overwritten.
        """
        with self._lock:
            try:
                if account_id is not None and account_expected_version is not None:
                    row = self._conn.execute(
                        "SELECT payload_json FROM accounts WHERE account_id=?", (account_id,)
                    ).fetchone()
                    current = None if row is None else json.loads(row["payload_json"])["state_version"]
                    if current != account_expected_version:
                        raise StaleAccountError(f"{account_expected_version}!={current}")
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
                if account_id is not None and account is not None:
                    self._save_account(account_id, account)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

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

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, status, terminal_reason, payload_json FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": run_id,
            "task_id": row["task_id"],
            "status": row["status"],
            "terminal_reason": row["terminal_reason"],
            "payload": json.loads(row["payload_json"]),
        }

    # --- L7: persisted simulated accounts and review cards ----------------------

    def _save_account(self, account_id: str, account: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO accounts(account_id, payload_json) VALUES (?, ?)
            ON CONFLICT(account_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (account_id, json.dumps(account)),
        )

    def put_account(self, account_id: str, account: dict[str, Any]) -> None:
        with self._lock:
            self._save_account(account_id, account)
            self._conn.commit()

    def get_account(self, account_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def put_review(self, card: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reviews(card_id, run_id, status, revision, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET
                  status=excluded.status,
                  revision=excluded.revision,
                  payload_json=excluded.payload_json
                """,
                (
                    card["card_id"],
                    card["run_id"],
                    card["status"],
                    int(card["revision"]),
                    json.dumps(card, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def get_review(self, card_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM reviews WHERE card_id=?", (card_id,)
            ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def list_reviews(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT payload_json FROM reviews ORDER BY rowid"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT payload_json FROM reviews WHERE status=? ORDER BY rowid", (status,)
                ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def get_review_response(self, response_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_hash, result_json FROM review_responses WHERE response_id=?",
                (response_id,),
            ).fetchone()
        if row is None:
            return None
        return {"payload_hash": row["payload_hash"], "result": json.loads(row["result_json"])}

    def put_review_response(
        self, *, response_id: str, card_id: str, payload_hash: str, result: dict[str, Any]
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO review_responses(response_id, card_id, payload_hash, result_json)
                VALUES (?, ?, ?, ?)
                """,
                (response_id, card_id, payload_hash, json.dumps(result, ensure_ascii=False)),
            )
            self._conn.commit()

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
