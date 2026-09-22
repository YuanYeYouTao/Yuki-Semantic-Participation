"""Bounded CAS snapshots in a separate controller database. No host ORM dependency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .controller import State
from .models import Scope


class SnapshotConflict(RuntimeError):
    pass


class SnapshotStore:
    def __init__(
        self, path: Path, *, per_scope_bytes: int = 1024 * 1024, total_bytes: int = 16 * 1024 * 1024
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS snapshots(
            scope TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL,
            payload BLOB NOT NULL, PRIMARY KEY(scope, generation))""")
        self.per_scope_bytes = per_scope_bytes
        self.total_bytes = total_bytes

    def load(self, scope: Scope) -> tuple[int, State] | None:
        row = self.connection.execute(
            "SELECT revision,payload FROM snapshots WHERE scope=? AND generation=?",
            (scope.conversation_id, scope.generation),
        ).fetchone()
        if row is None:
            return None
        state = State.model_validate_json(row[1])
        if state.scope != scope:
            raise ValueError("snapshot_scope_mismatch")
        return row[0], state

    def save(self, state: State, *, expected_revision: int) -> int:
        payload = state.model_dump_json().encode("utf-8")
        if len(payload) > self.per_scope_bytes:
            raise ValueError("scope_snapshot_capacity")
        key = (state.scope.conversation_id, state.scope.generation)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT revision,length(payload) FROM snapshots WHERE scope=? AND generation=?",
                key,
            ).fetchone()
            if (current[0] if current else 0) != expected_revision:
                raise SnapshotConflict("snapshot_changed")
            used = self.connection.execute(
                "SELECT coalesce(sum(length(payload)),0) FROM snapshots"
            ).fetchone()[0]
            if used - (current[1] if current else 0) + len(payload) > self.total_bytes:
                raise ValueError("global_snapshot_capacity")
            revision = expected_revision + 1
            self.connection.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?) ON CONFLICT(scope,generation) "
                "DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                (*key, revision, payload),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return revision

    def close(self) -> None:
        self.connection.close()

    def delete_scope(self, scope: Scope, *, expected_revision: int) -> None:
        """Explicit host retirement only; never evict unresolved runs to meet a quota.

        The host must first archive/reconcile any accepted runs and boundary records.
        A generation change by itself does not authorize deleting their receipts.
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT revision FROM snapshots WHERE scope=? AND generation=?",
                (scope.conversation_id, scope.generation),
            ).fetchone()
            if (current[0] if current else 0) != expected_revision:
                raise SnapshotConflict("snapshot_changed")
            self.connection.execute(
                "DELETE FROM snapshots WHERE scope=? AND generation=?",
                (scope.conversation_id, scope.generation),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
