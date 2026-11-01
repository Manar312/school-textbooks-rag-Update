"""Job queue abstraction: services enqueue work through this interface; Celery is just one implementation."""
import uuid
from abc import ABC, abstractmethod

from app.core.errors import AppError


class JobQueue(ABC):
    @abstractmethod
    def enqueue_ingest(self, book_id: str, *, reindex_only: bool = False) -> None: ...


class CeleryJobQueue(JobQueue):
    def enqueue_ingest(self, book_id: str, *, reindex_only: bool = False) -> None:
        """Sends a CHAIN: extract the pages, and only if that succeeds, index them. `reindex_only` skips step 1.

        `request_id` is new for every call. Celery re-sending the same message keeps the same id (so it is
        recognised as a duplicate), while a new admin click gets a new id (so it is a genuinely new request)."""
        from celery import chain

        from app.workers.tasks import extract_book, index_book
        request_id = uuid.uuid4().hex
        try:
            if reindex_only:
                index_book.si(book_id, request_id).apply_async()
            else:
                chain(extract_book.si(book_id, request_id), index_book.si(book_id, request_id)).apply_async()
        except Exception as e:
            raise AppError("Job queue unavailable, try again shortly.", code="queue_unavailable",
                           status_code=503) from e
