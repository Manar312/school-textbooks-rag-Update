"""Beat maintenance jobs: pure decision functions + real-Postgres tests (skipped when TEST_DATABASE_URL is not set)."""
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Book
from app.repositories import BookRepository
from app.workers.maintenance import find_orphans, is_stuck, recover_stuck_books_async

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run real-Postgres tests")


# ---------------------------------------------------------------- pure functions
def test_find_orphans_rules():
    now, hour = 1_000_000.0, 3600.0
    known = str(uuid.uuid4())
    orphan = str(uuid.uuid4())
    files = {
        ".upload-old": now - 2 * hour,          # interrupted upload, old     -> delete
        ".upload-new": now - 60,                # upload in progress          -> keep
        f"{known}.pdf": now - 100 * hour,       # has a DB row                -> keep
        f"{orphan}.pdf": now - 100 * hour,      # no row, old                 -> delete
        f"{uuid.uuid4()}.pdf": now - 60,        # no row yet but brand new    -> keep (row may be created right now)
        "notes.txt": now - 100 * hour,          # not ours                    -> never touch
        "not-a-uuid.pdf": now - 100 * hour,     # not ours                    -> never touch
    }
    doomed = find_orphans(files, {known}, now, temp_max_age=hour, pdf_max_age=24 * hour)
    assert sorted(doomed) == sorted([".upload-old", f"{orphan}.pdf"])


def test_is_stuck_uses_different_limits_for_processing_and_pending():
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    kw = dict(processing_after=timedelta(minutes=30), pending_after=timedelta(minutes=180))
    ago = lambda m: now - timedelta(minutes=m)  # noqa: E731
    assert is_stuck("processing", ago(45), now, **kw)
    assert not is_stuck("processing", ago(10), now, **kw)
    assert not is_stuck("pending", ago(45), now, **kw)       # may just be waiting in the queue
    assert is_stuck("pending", ago(240), now, **kw)
    assert not is_stuck("ready", ago(10_000), now, **kw)
    assert not is_stuck("failed", ago(10_000), now, **kw)


# ---------------------------------------------------------------- real Postgres
@pytest.fixture(scope="module")
def migrated_db():
    env = {**os.environ, "DATABASE_URL": TEST_DB, "JWT_SECRET": "x", "PYTHONPATH": str(ROOT)}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True, capture_output=True)
    return env


@needs_db
def test_alembic_migration_matches_the_models_exactly(migrated_db):
    out = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=ROOT, env=migrated_db, capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr
    assert "No new upgrade operations detected" in out.stdout + out.stderr


async def _add_book(repo, status, age_minutes, name):
    b = await repo.add(Book(id=uuid.uuid4(), title=name, subject="science", grade="prep-1", language="ar",
                            status=status, file_path=f"/x/{name}.pdf", file_sha256=uuid.uuid4().hex * 2))
    await repo.s.execute(text("UPDATE books SET updated_at = now() - make_interval(mins => :m) WHERE id = :i"),
                         {"m": age_minutes, "i": b.id})
    await repo.s.commit()
    return str(b.id)


@needs_db
async def test_recover_stuck_books_requeues_only_the_truly_stuck(migrated_db, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "database_url", TEST_DB)
    engine = create_async_engine(TEST_DB)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        repo = BookRepository(s)
        stuck = await _add_book(repo, "processing", 120, "stuck")
        locked = await _add_book(repo, "processing", 120, "slow-but-alive")
        await _add_book(repo, "processing", 5, "fresh")
        await _add_book(repo, "pending", 60, "waiting-in-queue")
        old_pending = await _add_book(repo, "pending", 400, "lost-pending")
        await _add_book(repo, "ready", 9999, "done")
    queued: list[str] = []
    recovered = await recover_stuck_books_async(lambda b: b == locked, queued.append)
    assert sorted(recovered) == sorted([stuck, old_pending]) and sorted(queued) == sorted(recovered)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        assert (await BookRepository(s).get(uuid.UUID(stuck))).status == "pending"
    await engine.dispose()


@needs_db
async def test_all_ids_and_list_stuck(migrated_db):
    engine = create_async_engine(TEST_DB)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        repo = BookRepository(s)
        old = await _add_book(repo, "processing", 500, "old")
        assert uuid.UUID(old) in await repo.all_ids()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
        assert old in [str(b.id) for b in await repo.list_stuck(cutoff)]
    await engine.dispose()
