"""SQLite database operations for financial data storage."""

import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS financial_data (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_name TEXT NOT NULL,
                    year        INTEGER NOT NULL,
                    revenue     REAL,
                    net_profit  REAL,
                    assets      REAL,
                    liabilities REAL,
                    equity      REAL,
                    scraped_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (company_name, year)
                );

                CREATE TABLE IF NOT EXISTS scrape_progress (
                    company_name        TEXT PRIMARY KEY,
                    registration_number TEXT,
                    status              TEXT NOT NULL,  -- 'done' | 'failed' | 'no_data'
                    processed_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    error_message       TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_fin_company ON financial_data(company_name);
                CREATE INDEX IF NOT EXISTS idx_fin_year    ON financial_data(year);
            """)
        logger.debug("Database initialised at %s", self.db_path)

    # ------------------------------------------------------------------ writes

    def upsert_financial_record(self, record: dict) -> bool:
        """Insert or replace a single financial record. Returns True on insert."""
        sql = """
            INSERT INTO financial_data
                (company_name, year, revenue, net_profit, assets, liabilities, equity, scraped_at)
            VALUES
                (:company_name, :year, :revenue, :net_profit, :assets, :liabilities, :equity, :scraped_at)
            ON CONFLICT(company_name, year) DO UPDATE SET
                revenue     = excluded.revenue,
                net_profit  = excluded.net_profit,
                assets      = excluded.assets,
                liabilities = excluded.liabilities,
                equity      = excluded.equity,
                scraped_at  = excluded.scraped_at
        """
        record.setdefault("scraped_at", datetime.utcnow())
        with self._conn() as conn:
            cur = conn.execute(sql, record)
            inserted = cur.rowcount > 0
        return inserted

    def upsert_many(self, records: list[dict]) -> int:
        """Bulk upsert. Returns number of rows affected."""
        if not records:
            return 0
        for r in records:
            r.setdefault("scraped_at", datetime.utcnow())
        sql = """
            INSERT INTO financial_data
                (company_name, year, revenue, net_profit, assets, liabilities, equity, scraped_at)
            VALUES
                (:company_name, :year, :revenue, :net_profit, :assets, :liabilities, :equity, :scraped_at)
            ON CONFLICT(company_name, year) DO UPDATE SET
                revenue     = excluded.revenue,
                net_profit  = excluded.net_profit,
                assets      = excluded.assets,
                liabilities = excluded.liabilities,
                equity      = excluded.equity,
                scraped_at  = excluded.scraped_at
        """
        with self._conn() as conn:
            cur = conn.executemany(sql, records)
            return cur.rowcount

    def mark_progress(self, company_name: str, registration_number: Optional[str],
                      status: str, error_message: Optional[str] = None):
        sql = """
            INSERT INTO scrape_progress (company_name, registration_number, status, processed_at, error_message)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(company_name) DO UPDATE SET
                status        = excluded.status,
                processed_at  = excluded.processed_at,
                error_message = excluded.error_message
        """
        with self._conn() as conn:
            conn.execute(sql, (company_name, registration_number, status,
                               datetime.utcnow(), error_message))

    # ------------------------------------------------------------------ reads

    def get_processed_companies(self) -> set[str]:
        """Return company names already marked 'done'."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT company_name FROM scrape_progress WHERE status = 'done'"
            ).fetchall()
        return {r["company_name"] for r in rows}

    def get_all_financial_data(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM financial_data ORDER BY company_name, year"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_exists(self, company_name: str, year: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM financial_data WHERE company_name=? AND year=?",
                (company_name, year),
            ).fetchone()
        return row is not None
