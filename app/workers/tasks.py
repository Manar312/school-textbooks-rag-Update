"""Celery tasks. Celery runs plain (synchronous) functions, while our services are async, so every task calls
`asyncio.run(...)` once and builds fresh engine + providers inside it (an async client must not outlive its event loop).

Ingestion = a CHAIN of two idempotent tasks:  extract_book (slow, VLM)  ->  index_book (embeddings + vector store).
"""
import asyncio
import logging
import uuid

import redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.container import Container
from app.core.db import make_engine
from app.repositories import BookRepository
from app.services.ingestion import IngestionService
from app.workers.celery_app import celery_app
from app.workers.idempotency import IdempotencyManager, Outcome
from app.workers.maintenance import cleanup_orphan_files_async, recover_stuck_books_async
from app.workers.queue import CeleryJobQueue

log = logging.getLogger(__name__)


def make_manager() -> IdempotencyManager:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return IdempotencyManager(client, lease_ttl=settings.task_lease_ttl_seconds, done_ttl=settings.task_done_ttl_seconds)


def run_idempotent(task, name: str, book_id: str, request_id: str, work, manager: IdempotencyManager | None = None):
    """Runs `work()` at most once per (task, book, request) and never two ingestions of the same book at once.

    1) payload signature: skip if this exact request is running or finished.
    2) book lock: if ANOTHER request is working on the same book, wait (Celery retry) instead of racing it.
    3) on failure free both locks so Celery's retry can run again; on success remember "done"."""
    mgr = manager or make_manager()
    sig = mgr.signature(name, {"book_id": book_id, "request_id": request_id})
    outcome, lease = mgr.begin(sig)
    if outcome is Outcome.DONE:
        return {"status": "skipped", "reason": "already done", "book_id": book_id}
    if outcome is Outcome.RUNNING:
        return {"status": "skipped", "reason": "already running", "book_id": book_id}
    book_lock = mgr.acquire(f"book:{book_id}")
    if book_lock is None:
        mgr.abort(lease)
        raise task.retry(countdown=120, max_retries=30)
    try:
        result = work()
    except Exception as exc:
        book_lock.release()
        mgr.abort(lease)
        raise task.retry(exc=exc)
    book_lock.release()
    mgr.finish(sig, lease)
    return result


async def _with_service(roles: tuple[str, ...], method: str, book_id: str):
    engine = make_engine(null_pool=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    container = Container(settings, sessionmaker=sm)
    try:
        container.providers.validate(roles)  # a bad key/model in .env fails here with a clear message
        async with sm() as s:
            svc = IngestionService(BookRepository(s), container.open_book_repo, container.providers)
            return await getattr(svc, method)(uuid.UUID(book_id))
    finally:
        await container.providers.aclose()
        await engine.dispose()


@celery_app.task(name="ingest.extract", bind=True, max_retries=2, default_retry_delay=60)
def extract_book(self, book_id: str, request_id: str):
    return run_idempotent(self, "ingest.extract", book_id, request_id,
                          lambda: asyncio.run(_with_service(("vlm", "ocr"), "extract", book_id)))


@celery_app.task(name="ingest.index", bind=True, max_retries=2, default_retry_delay=60)
def index_book(self, book_id: str, request_id: str):
    return run_idempotent(self, "ingest.index", book_id, request_id,
                          lambda: asyncio.run(_with_service(("vectors", "embedder"), "index", book_id)))


@celery_app.task(name="maintenance.cleanup_orphan_files")
def cleanup_orphan_files():
    return asyncio.run(cleanup_orphan_files_async())


@celery_app.task(name="maintenance.recover_stuck_books")
def recover_stuck_books():
    mgr, queue = make_manager(), CeleryJobQueue()
    return asyncio.run(recover_stuck_books_async(lambda b: mgr.is_locked(f"book:{b}"),
                                                 lambda b: queue.enqueue_ingest(b)))
