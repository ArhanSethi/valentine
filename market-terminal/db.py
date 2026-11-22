"""SQLite schema and a single dedicated writer thread.

Stream callbacks never touch SQLite directly. They put rows on a queue, and
one writer thread owns the only write connection. That avoids SQLite's
thread-binding errors without a lock, and batching commits keeps up with
bursts of trades.
"""

import queue
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    symbol    TEXT NOT NULL,
    timestamp TEXT NOT NULL,   -- ISO 8601, UTC, microsecond precision
    price     REAL NOT NULL,
    size      REAL NOT NULL,
    market    TEXT NOT NULL    -- 'stock' or 'crypto'
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades (symbol, timestamp);

CREATE TABLE IF NOT EXISTS feed_status (
    feed         TEXT PRIMARY KEY,  -- 'stock' or 'crypto'
    state        TEXT NOT NULL,     -- 'connecting', 'streaming', 'error', 'demo'
    last_message TEXT,              -- ISO time the last trade arrived (UTC)
    detail       TEXT
);
"""


def connect(path, readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=5)
        # WAL lets the dashboard read while the collector writes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    return conn


class DbWriter(threading.Thread):
    """Drains a queue of write jobs and commits them in small batches."""

    def __init__(self, path, flush_interval=0.25):
        super().__init__(name="db-writer", daemon=True)
        self.path = path
        self.flush_interval = flush_interval
        self.jobs = queue.Queue()
        self.ready = threading.Event()

    def add_trade(self, symbol, timestamp, price, size, market):
        self.jobs.put(("trade", (symbol, timestamp, price, size, market)))

    def set_status(self, feed, state, last_message=None, detail=None):
        self.jobs.put(("status", (feed, state, last_message, detail)))

    def run(self):
        conn = connect(self.path)
        self.ready.set()
        while True:
            batch = [self.jobs.get()]
            deadline = time.monotonic() + self.flush_interval
            while time.monotonic() < deadline:
                try:
                    batch.append(self.jobs.get(timeout=max(0, deadline - time.monotonic())))
                except queue.Empty:
                    break
            trades = [row for kind, row in batch if kind == "trade"]
            statuses = [row for kind, row in batch if kind == "status"]
            with conn:
                if trades:
                    conn.executemany("INSERT INTO trades VALUES (?, ?, ?, ?, ?)", trades)
                for feed, state, last_message, detail in statuses:
                    conn.execute(
                        """
                        INSERT INTO feed_status (feed, state, last_message, detail)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(feed) DO UPDATE SET
                            state = excluded.state,
                            last_message = COALESCE(excluded.last_message, feed_status.last_message),
                            detail = excluded.detail
                        """,
                        (feed, state, last_message, detail),
                    )
