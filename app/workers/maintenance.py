"""Periodic maintenance jobs that Celery Beat (the scheduler) sends on a timetable.

* cleanup_orphan_files  - deletes half-uploaded temp files and PDFs that no book row refers to.
* recover_stuck_books   - finds books left "processing/pending" by a crashed worker and re-queues them.

The decision logic lives in small pure functions (`find_orphans`, `is_stuck`) so it can be unit-tested without
a database, a clock or a disk.
"""
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import settings

log = logging.getLogger(__name__)

TEMP_PREFIX = ".upload-"


def find_orphans(files: dict[str, float], known_ids: set[str], now: float, *, temp_max_age: float,
                 pdf_max_age: float) -> list[str]:
    """files = {file name: last-modified unix time}. Returns the names that are safe to delete.

    * `.upload-xxxx`      : an upload that was interrupted. Deleted once older than `temp_max_age`.
    * `<book-uuid>.pdf`   : deleted only if NO book row has that id AND it is older than `pdf_max_age`
                            (the age check protects a book whose DB row is being created right now).
    * anything else       : never touched.
    """
    doomed = []
    for name, mtime in files.items():
        age = now - mtime
        if name.startswith(TEMP_PREFIX):
            if age > temp_max_age:
                doomed.append(name)
        elif name.endswith(".pdf"):
            stem = name[:-4]
            try:
                uuid.UUID(stem)
            except ValueError:
                continue
            if stem not in known_ids and age > pdf_max_age:
                doomed.append(name)
    return doomed


def is_stuck(status: str, updated_at: datetime, now: datetime, *, processing_after: timedelta,
             pending_after: timedelta) -> bool:
    """A book is stuck when it claims to be working but nothing touched its row recently.

    `processing` books update their row every ~10 pages, so silence means a dead worker -> short limit.
    `pending` books may simply be waiting in a long queue behind another book -> much longer limit, otherwise we
    would re-queue books that are only waiting their turn."""
    age = now - updated_at
    if status == "processing":
        return age > processing_after
    if status == "pending":
        return age > pending_after
    return False


async def cleanup_orphan_files_async() -> list[str]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.db import make_engine
    from app.repositories import BookRepository

    folder = os.path.join(settings.storage_dir, "books")
    if not os.path.isdir(folder):
        return []
    files = {n: os.path.getmtime(os.path.join(folder, n)) for n in os.listdir(folder)
             if os.path.isfile(os.path.join(folder, n))}
    engine = make_engine(null_pool=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            known = {str(i) for i in await BookRepository(s).all_ids()}
    finally:
        await engine.dispose()
    doomed = find_orphans(files, known, time.time(), temp_max_age=3600, pdf_max_age=settings.orphan_file_hours * 3600)
    for name in doomed:
        try:
            os.remove(os.path.join(folder, name))
        except FileNotFoundError:
            pass
    if doomed:
        log.info("cleanup removed %s orphan file(s): %s", len(doomed), doomed)
    return doomed


async def recover_stuck_books_async(is_locked, enqueue) -> list[str]:
    """`is_locked(book_id) -> bool` and `enqueue(book_id)` are passed in, so this function never needs Redis or Celery."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.db import make_engine
    from app.repositories import BookRepository

    engine = make_engine(null_pool=True)
    recovered: list[str] = []
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            repo = BookRepository(s)
            now = datetime.now(timezone.utc)
            processing_after = timedelta(minutes=settings.stuck_book_minutes)
            pending_after = timedelta(minutes=settings.stuck_pending_minutes)
            for book in await repo.list_stuck(now - min(processing_after, pending_after)):
                if not is_stuck(book.status, book.updated_at, now, processing_after=processing_after,
                                pending_after=pending_after):
                    continue
                if is_locked(str(book.id)):  # a live worker still holds the book: it is slow, not stuck
                    continue
                await repo.update(book.id, status="pending", error="recovered after a worker stopped unexpectedly")
                enqueue(str(book.id))
                recovered.append(str(book.id))
    finally:
        await engine.dispose()
    if recovered:
        log.warning("re-queued %s stuck book(s): %s", len(recovered), recovered)
    return recovered
