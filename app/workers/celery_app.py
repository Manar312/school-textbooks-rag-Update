"""The Celery application: one object that knows the broker (Redis), the settings and the Beat timetable."""
import logging

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

celery_app = Celery("school_rag", broker=settings.redis_url, backend=settings.redis_url,
                    include=["app.workers.tasks"])
celery_app.conf.update(
    task_acks_late=True,                # a job leaves the queue only when it FINISHES: a dead worker => job is re-delivered
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,       # take one job at a time: a 3-hour book must not hide jobs from other workers
    task_default_queue="ingest",
    task_routes={"maintenance.*": {"queue": "maintenance"}},  # Beat jobs must never wait behind a 3-hour book
    task_soft_time_limit=60 * 60 * 3,
    task_time_limit=60 * 60 * 3 + 300,
    result_expires=86400,
    broker_transport_options={"visibility_timeout": 60 * 60 * 4},
    timezone=settings.timezone,
    # Beat = the scheduler process. It only SENDS these jobs on time; the "maintenance" worker runs them.
    beat_schedule={
        "cleanup-orphan-files": {"task": "maintenance.cleanup_orphan_files", "schedule": crontab(minute=17),
                                 "options": {"queue": "maintenance"}},
        "recover-stuck-books": {"task": "maintenance.recover_stuck_books", "schedule": 600.0,
                                "options": {"queue": "maintenance"}},
    },
)
