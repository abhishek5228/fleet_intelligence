"""A dict that lives in SQLite and is safe to share between threads."""
import collections.abc
import json
import os
import re
import sqlite3
import threading
from typing import Any, Iterator, Optional

from fleet.config import cfg

_TABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PersistentDict(collections.abc.MutableMapping):
    """Drop-in MutableMapping backed by SQLite, so state survives restarts and redeploys.

    Values are JSON-serialised: `.get()` returns a COPY, so mutate it and assign it back
    (`d[k] = value`). Each statement runs under a lock because one sqlite3 connection must
    never be used by two threads at once. Compound read-modify-write sequences across several
    statements additionally need `fleet.state.STATE_LOCK`.
    """

    def __init__(self, table_name: str, db_path: Optional[str] = None):
        if not _TABLE_NAME.fullmatch(table_name):
            raise ValueError(f"invalid table name: {table_name!r}")
        self.table = table_name
        self._lock = threading.RLock()
        path = db_path or cfg.DB_PATH
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self._lock:
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (key TEXT PRIMARY KEY, value TEXT)")
            self._conn.commit()

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            row = self._conn.execute(f"SELECT value FROM {self.table} WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key: str, value: Any) -> None:
        payload = json.dumps(value)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self.table} (key, value) VALUES (?, ?) "
                f"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )
            self._conn.commit()

    def __delitem__(self, key: str) -> None:
        with self._lock:
            cur = self._conn.execute(f"DELETE FROM {self.table} WHERE key=?", (key,))
            self._conn.commit()
            removed = cur.rowcount
        if removed == 0:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            keys = [r[0] for r in self._conn.execute(f"SELECT key FROM {self.table}").fetchall()]
        return iter(keys)

    def __len__(self) -> int:
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return self._conn.execute(f"SELECT 1 FROM {self.table} WHERE key=?", (key,)).fetchone() is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
