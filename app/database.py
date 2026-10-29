"""Access to the local SQLite database selected by the user."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path


class SQLiteDatabase:
    """Hands out short-lived connections to a local SQLite file."""

    REQUIRED_TABLE = "lora_measurement"

    def __init__(self, path: Path):
        self.path = path
        if not self.path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {self.path}")
        self._check_schema()
        self._ensure_indexes()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def _check_schema(self) -> None:
        try:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0) as con:
                found = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.REQUIRED_TABLE,)
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise ValueError(f"Not a valid SQLite database: {self.path}\n{error}") from error
        if not found:
            raise ValueError(f"The database has no '{self.REQUIRED_TABLE}' table: {self.path}")

    def _ensure_indexes(self) -> None:
        try:
            with sqlite3.connect(self.path, timeout=10.0) as con:
                con.execute("CREATE INDEX IF NOT EXISTS idx_lora_measurement_pc_ts ON lora_measurement(pc_ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_lora_measurement_link_time ON lora_measurement(sender_id, receiver_id, pc_ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_lora_measurement_ok_time ON lora_measurement(ok_match, pc_ts)")
        except sqlite3.OperationalError as e:
            # E.g. a read-only file or filesystem: queries still work, just slower.
            logging.warning("Could not create SQLite indexes: %s", e)
