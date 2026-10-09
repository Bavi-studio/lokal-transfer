from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    stored_name TEXT NOT NULL UNIQUE,
                    size INTEGER NOT NULL CHECK(size >= 0),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def insert_file(self, file_id: str, name: str, stored_name: str, size: int) -> dict[str, Any]:
        created_at = self.now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO files(id, name, stored_name, size, created_at) VALUES (?, ?, ?, ?, ?)",
                (file_id, name, stored_name, size, created_at),
            )
        return {"id": file_id, "name": name, "size": size, "created_at": created_at}

    def list_files(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id, name, size, created_at FROM files ORDER BY created_at DESC, id").fetchall()
        return [dict(row) for row in rows]

    def total_file_size(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(SUM(size), 0) AS total FROM files").fetchone()
        return int(row["total"])

    def stored_files(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id, stored_name FROM files").fetchall()
        return [dict(row) for row in rows]

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def delete_file_record(self, file_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        return cursor.rowcount > 0

    def add_message(self, text: str, history_limit: int) -> dict[str, Any]:
        created_at = self.now()
        with self._connect() as db:
            cursor = db.execute("INSERT INTO messages(text, created_at) VALUES (?, ?)", (text, created_at))
            message_id = cursor.lastrowid
            db.execute(
                "DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
                (history_limit,),
            )
        return {"id": message_id, "text": text, "created_at": created_at}

    def list_messages(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, text, created_at FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in reversed(rows)]
