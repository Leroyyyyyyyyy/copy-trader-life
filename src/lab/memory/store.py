"""SQLite memory with publish/overlay isolation. Labels never enter content."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from src.lab.domain import MemoryRecord, MemoryStatus, MemoryType

FORBIDDEN_LABEL_KEYS = (
    "acceptable_outcomes",
    "gold",
    "gold_label",
    "hidden_label",
    "eval_spec",
    "expected_stop_loss",
)


class LabelInMemoryError(ValueError):
    """Raised when a gold label is proposed as a memory."""


class MemoryStore:
    def __init__(self, db_path: Path | str, *, snapshot_id: str = "default"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_id = snapshot_id
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._overlay: list[MemoryRecord] = []
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
              memory_id TEXT PRIMARY KEY,
              snapshot_id TEXT NOT NULL,
              memory_type TEXT NOT NULL,
              scope TEXT NOT NULL,
              content TEXT NOT NULL,
              source_event_id TEXT,
              confidence REAL NOT NULL,
              valid_from TEXT,
              valid_to TEXT,
              version INTEGER NOT NULL,
              status TEXT NOT NULL,
              supersedes TEXT
            );
            """
        )
        self._conn.commit()

    def _row(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            memory_type=MemoryType(row["memory_type"]),
            scope=row["scope"],
            content=row["content"],
            source_event_id=row["source_event_id"],
            confidence=float(row["confidence"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            version=int(row["version"]),
            status=MemoryStatus(row["status"]),
            supersedes=row["supersedes"],
            snapshot_id=row["snapshot_id"],
        )

    def publish(self, record: MemoryRecord) -> MemoryRecord:
        self._assert_no_label(record.content)
        rec = MemoryRecord(
            memory_id=record.memory_id or f"mem_{uuid.uuid4().hex[:12]}",
            memory_type=record.memory_type,
            scope=record.scope,
            content=record.content,
            source_event_id=record.source_event_id,
            confidence=record.confidence,
            valid_from=record.valid_from,
            valid_to=record.valid_to,
            version=record.version,
            status=record.status,
            supersedes=record.supersedes,
            snapshot_id=record.snapshot_id or self.snapshot_id,
        )
        self._conn.execute(
            """
            INSERT INTO memories(
              memory_id, snapshot_id, memory_type, scope, content, source_event_id,
              confidence, valid_from, valid_to, version, status, supersedes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.memory_id,
                rec.snapshot_id,
                rec.memory_type.value,
                rec.scope,
                rec.content,
                rec.source_event_id,
                rec.confidence,
                rec.valid_from,
                rec.valid_to,
                rec.version,
                rec.status.value,
                rec.supersedes,
            ),
        )
        self._conn.commit()
        return rec

    def propose(self, record: MemoryRecord) -> MemoryRecord:
        """Run overlay only. Next experiment does not see this unless publish() is called."""
        self._assert_no_label(record.content)
        rec = MemoryRecord(
            memory_id=record.memory_id or f"cand_{uuid.uuid4().hex[:12]}",
            memory_type=record.memory_type,
            scope=record.scope,
            content=record.content,
            source_event_id=record.source_event_id,
            confidence=record.confidence,
            valid_from=record.valid_from,
            valid_to=record.valid_to,
            version=record.version,
            status=MemoryStatus.CANDIDATE,
            supersedes=record.supersedes,
            snapshot_id=self.snapshot_id,
        )
        self._overlay.append(rec)
        return rec

    def supersede(self, old_id: str, new: MemoryRecord) -> MemoryRecord:
        self._conn.execute(
            "UPDATE memories SET status=? WHERE memory_id=?",
            (MemoryStatus.SUPERSEDED.value, old_id),
        )
        published = self.publish(
            MemoryRecord(
                memory_id=new.memory_id or f"mem_{uuid.uuid4().hex[:12]}",
                memory_type=new.memory_type,
                scope=new.scope,
                content=new.content,
                source_event_id=new.source_event_id,
                confidence=new.confidence,
                valid_from=new.valid_from,
                valid_to=new.valid_to,
                version=new.version,
                status=MemoryStatus.ACTIVE,
                supersedes=old_id,
                snapshot_id=new.snapshot_id or self.snapshot_id,
            )
        )
        return published

    def retrieve(
        self,
        *,
        query: str = "",
        scope: Optional[str] = None,
        visible_at: Optional[str] = None,
        include_overlay: bool = True,
        snapshot_id: Optional[str] = None,
    ) -> list[MemoryRecord]:
        snap = snapshot_id or self.snapshot_id
        rows = self._conn.execute(
            """
            SELECT * FROM memories
            WHERE snapshot_id=? AND status IN (?, ?)
            """,
            (snap, MemoryStatus.ACTIVE.value, MemoryStatus.CANDIDATE.value),
        ).fetchall()
        hits = [self._row(r) for r in rows]
        if include_overlay:
            hits.extend(self._overlay)
        out: list[MemoryRecord] = []
        q = query.lower().strip()
        for rec in hits:
            if rec.status in {MemoryStatus.SUPERSEDED, MemoryStatus.EXPIRED, MemoryStatus.DELETED}:
                continue
            if rec.valid_to and visible_at and visible_at > rec.valid_to:
                continue
            if rec.valid_from and visible_at and visible_at < rec.valid_from:
                continue
            if scope and rec.scope != scope and rec.scope != "*":
                continue
            if q and q not in rec.content.lower() and q not in rec.scope.lower():
                continue
            out.append(rec)
        return out

    def copy_snapshot(self, dest_path: Path | str, snapshot_id: str) -> "MemoryStore":
        """Independent experiment copy of published rows only (no overlay)."""
        dest = MemoryStore(dest_path, snapshot_id=snapshot_id)
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE snapshot_id=? AND status=?",
            (self.snapshot_id, MemoryStatus.ACTIVE.value),
        ).fetchall()
        for row in rows:
            rec = self._row(row)
            dest.publish(
                MemoryRecord(
                    memory_id=rec.memory_id,
                    memory_type=rec.memory_type,
                    scope=rec.scope,
                    content=rec.content,
                    source_event_id=rec.source_event_id,
                    confidence=rec.confidence,
                    valid_from=rec.valid_from,
                    valid_to=rec.valid_to,
                    version=rec.version,
                    status=MemoryStatus.ACTIVE,
                    supersedes=rec.supersedes,
                    snapshot_id=snapshot_id,
                )
            )
        return dest

    def _assert_no_label(self, content: str) -> None:
        lowered = content.lower()
        for key in FORBIDDEN_LABEL_KEYS:
            if key in lowered:
                raise LabelInMemoryError(f"memory must not contain {key}")


def memories_for_prompt(records: Iterable[MemoryRecord]) -> list[dict[str, Any]]:
    return [r.to_agent_dict() for r in records]
