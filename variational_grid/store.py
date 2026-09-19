"""Durable paper ledger; one writer and atomic paired fills."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3

from .models import D, GridError, dec


def fill_totals(db, through=None):
    """Exact gross volume, both legs and both phases; never sum TEXT as SQLite floats."""
    qty, notional, count = D(0), D(0), 0
    query = "SELECT qty,price FROM fills"
    for amount, price in db.execute(query + (" WHERE ts<=?" if through is not None else ""),
                                    (through,) if through is not None else ()):
        qty += dec(amount)
        notional += dec(amount) * dec(price)
        count += 1
    return {"volume_barrels": str(qty), "turnover_usdc": str(notional), "fill_count": count}


class ProcessLock:
    def __init__(self, path):
        self.path = Path(str(path) + ".lock")
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise GridError("Another process already owns this ledger") from None
        return self

    def __exit__(self, *_):
        self.handle.close()


class Store:
    def __init__(self, path, config):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lots (
          id INTEGER PRIMARY KEY, direction INTEGER NOT NULL, level INTEGER NOT NULL,
          qty TEXT NOT NULL, entry_center TEXT NOT NULL, entry_cl TEXT NOT NULL,
          entry_bz TEXT NOT NULL, entry_fee TEXT NOT NULL, opened REAL NOT NULL,
          closed REAL, exit_cl TEXT, exit_bz TEXT, net_pnl TEXT, reason TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS active_level ON lots(direction, level) WHERE closed IS NULL;
        CREATE TABLE IF NOT EXISTS fills (
          id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id),
          ts REAL NOT NULL, phase TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
          qty TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ticks (id INTEGER PRIMARY KEY, ts REAL NOT NULL, snapshot TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ticks_time ON ticks(ts);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
        """)
        identity = self.get("config")
        if identity is not None and identity != config.strategy_identity():
            self.close()
            raise GridError("Strategy settings differ from this ledger; use a new state_file for changed settings")
        if identity is None:
            with self.transaction():
                self.set("config", config.strategy_identity())
                self.set("cash", config.paper_balance_usdc)
                self.set("peak", config.paper_balance_usdc)
                self.set("halted", "")
                self.set("blocked", "[]")
                self.set("fees", "0")
                self.set("realized", "0")
                self.set("schema_version", "1")
        if self.get("volume_barrels") is None:
            # One-time backfill for old ledgers, from durable executions only.
            with self.transaction():
                for key, value in fill_totals(self.db).items():
                    self.set(key, value)

    def volume(self):
        return {"volume_barrels": self.get("volume_barrels"),
                "turnover_usdc": self.get("turnover_usdc"),
                "fill_count": int(self.get("fill_count"))}

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def lots(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM lots WHERE closed IS NULL ORDER BY id")]

    def event(self, ts, kind, message):
        self.db.execute("INSERT INTO events(ts,kind,message) VALUES (?,?,?)", (ts, kind, message))

    def snapshot(self):
        row = self.db.execute("SELECT snapshot FROM ticks ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
