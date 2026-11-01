"""Production plumbing: metrics, request middleware, config safety, Celery wiring."""
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings


@pytest.fixture(scope="module")
def client():
    """The real app with only the heavy startup (providers, Redis, Qdrant) swapped for nothing."""
    from contextlib import asynccontextmanager

    from app.main import create_app

    @asynccontextmanager
    async def light_lifespan(app):
        from types import SimpleNamespace
        class _NoDb:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        # Routes that reject a missing token never really use the container or the DB session.
        app.state.container = SimpleNamespace(sessionmaker=lambda: _NoDb())
        yield

    app = create_app()
    app.router.lifespan_context = light_lifespan
    with TestClient(app) as c:
        yield c


def test_every_response_has_a_request_id_and_a_given_one_is_kept(client):
    assert len(client.get("/health/live").headers["x-request-id"]) >= 8
    assert client.get("/health/live", headers={"x-request-id": "abc-123"}).headers["x-request-id"] == "abc-123"


def test_metrics_use_route_templates_not_real_ids(client):
    sid = uuid.uuid4()
    client.get(f"/chat/sessions/{sid}/messages")            # 401: no token, but the route was matched
    client.get("/auth/me")
    body = client.get("/metrics").text
    assert 'path="/chat/sessions/{sid}/messages"' in body
    assert str(sid) not in body                              # no per-student series (cardinality explosion)
    assert re.search(r'http_requests_total\{method="GET",path="/auth/me",status="401"\} \d+', body)
    assert "http_request_duration_seconds_bucket" in body


def test_metrics_endpoint_does_not_count_itself_or_liveness_probes(client):
    client.get("/metrics")
    body = client.get("/metrics").text
    assert 'path="/metrics"' not in body and 'path="/health/live"' not in body


def test_business_metrics_are_exposed(client):
    body = client.get("/metrics").text
    from app.core import metrics
    metrics.QUESTIONS.labels("llm", "answered").inc()
    assert 'rag_questions_total{outcome="answered",route_method="llm"}' in client.get("/metrics").text


def test_unknown_urls_are_grouped_not_one_series_per_url(client):
    client.get("/no/such/page-1")
    client.get("/no/such/page-2")
    body = client.get("/metrics").text
    assert "page-1" not in body and 'path="unmatched"' in body


# ---------------------------------------------------------------- config safety
def test_prod_refuses_weak_secret_and_open_cors():
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(app_env="prod", jwt_secret="short", cors_origins="https://school.example")
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(app_env="prod", jwt_secret="x" * 40, cors_origins="*")
    ok = Settings(app_env="prod", jwt_secret="x" * 40, cors_origins="https://school.example")
    assert ok.app_env == "prod"


def test_dev_stays_easy():
    assert Settings(jwt_secret="x").app_env == "dev"


# ---------------------------------------------------------------- celery wiring
def test_tasks_and_beat_schedule_are_registered():
    from app.workers.celery_app import celery_app
    import app.workers.tasks  # noqa: F401
    names = set(celery_app.tasks)
    assert {"ingest.extract", "ingest.index", "maintenance.cleanup_orphan_files",
            "maintenance.recover_stuck_books"} <= names
    sched = celery_app.conf.beat_schedule
    assert {v["task"] for v in sched.values()} == {"maintenance.cleanup_orphan_files", "maintenance.recover_stuck_books"}
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_routes["maintenance.*"]["queue"] == "maintenance"


def test_ingest_is_a_chain_extract_then_index_and_reindex_skips_extract(monkeypatch):
    from celery import canvas

    from app.workers.queue import CeleryJobQueue
    sent = []
    monkeypatch.setattr(canvas._chain, "apply_async", lambda self, *a, **k: sent.append(("chain", [t.task for t in self.tasks])))
    monkeypatch.setattr(canvas.Signature, "apply_async", lambda self, *a, **k: sent.append(("single", self.task, self.args)))
    q = CeleryJobQueue()
    q.enqueue_ingest("book-1")
    q.enqueue_ingest("book-1", reindex_only=True)
    assert sent[0] == ("chain", ["ingest.extract", "ingest.index"])
    assert sent[1][0] == "single" and sent[1][1] == "ingest.index"
    assert sent[1][2][0] == "book-1" and len(sent[1][2][1]) == 32        # book id + fresh request id
