"""SQLite engine, sessions, and schema creation (including the FTS5 table).

Transactions are managed explicitly: pysqlite's implicit BEGIN is disabled and we emit our own.
Stock-changing work uses `Database.write()`, which opens a `BEGIN IMMEDIATE` transaction so
concurrent writers queue on SQLite's write lock instead of failing with SQLITE_BUSY mid-transaction.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

TS_FMT = "%Y-%m-%d %H:%M:%S.%f"  # SQLAlchemy's SQLite DateTime storage format


def sql_ts(dt: datetime) -> str:
    """Format a datetime exactly as stored, for raw text() queries that compare timestamps as strings."""
    return dt.strftime(TS_FMT)


FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS alert_fts USING fts5("
    "alert_id UNINDEXED, headline, report_text, facts_text, tokenize='porter unicode61')"
)


def make_engine(path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        dbapi_conn.isolation_level = None  # we issue BEGIN ourselves (see _on_begin)
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn) -> None:  # type: ignore[no-untyped-def]
        conn.exec_driver_sql("BEGIN IMMEDIATE" if conn.get_execution_options().get("immediate") else "BEGIN")

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(FTS_DDL))


class Database:
    """Owns the engine and hands out transactional sessions."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.engine = make_engine(path)
        self._sessionmaker = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Regular (deferred) transaction: commit on success, roll back on error."""
        s = self._sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    @contextmanager
    def write(self) -> Iterator[Session]:
        """Serialised write transaction (BEGIN IMMEDIATE) for stock changes."""
        with self.engine.connect() as conn:
            conn.execution_options(immediate=True)
            s = Session(bind=conn, expire_on_commit=False)
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

    def dispose(self) -> None:
        self.engine.dispose()


@lru_cache
def get_database() -> Database:
    return Database(get_settings().database_path)
