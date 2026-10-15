from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from app.config import get_settings
from scripts.generate_data import generate

TEST_ANCHOR = datetime(2026, 10, 15, 10, 0)


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, dict]]:
    """A full synthetic database generated once per test session with a fixed seed and anchor."""
    path = tmp_path_factory.mktemp("data") / "seeded.db"
    summary = generate(str(path), seed=2026, anchor=TEST_ANCHOR,
                       calendar_file=get_settings().calendar_file, reset=True, quiet=True)
    yield path, summary


@pytest.fixture()
def con(seeded_db: tuple[Path, dict]) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(seeded_db[0])
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def work_db(seeded_db: tuple[Path, dict], tmp_path: Path) -> Path:
    """A private, writable copy of the seeded database for tests that mutate state."""
    dst = tmp_path / "work.db"
    src_con, dst_con = sqlite3.connect(seeded_db[0]), sqlite3.connect(dst)
    try:
        src_con.backup(dst_con)
    finally:
        src_con.close()
        dst_con.close()
    return dst
