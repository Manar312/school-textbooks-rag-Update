"""Prometheus metrics: what the app exposes at GET /metrics for Prometheus to scrape every few seconds.

Two kinds of numbers:
  * HTTP metrics (requests, latency, in-flight) filled by `RequestMiddleware`.
  * Business metrics (questions per router method, LLM tokens, errors) filled by `RagService`.
Queue depth is read from Redis at scrape time so Grafana can show how many books are waiting.

Several uvicorn workers = several processes, each with its own memory. Prometheus would then see a random
worker on every scrape. Setting PROMETHEUS_MULTIPROC_DIR makes prometheus_client write every worker's numbers
into files in that folder, and `render_metrics()` merges them into one answer.
"""
import os

from prometheus_client import (CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest,
                               multiprocess)
from prometheus_client.core import GaugeMetricFamily

from app.core.config import settings

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "Time until the response is fully sent",
                         ["method", "path"],
                         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
HTTP_IN_PROGRESS = Gauge("http_requests_in_progress", "Requests being handled right now",
                         multiprocess_mode="livesum")

QUESTIONS = Counter("rag_questions_total", "Student questions by how they were routed and how they ended",
                    ["route_method", "outcome"])
LLM_TOKENS = Counter("rag_llm_tokens_total", "Tokens used by answers (direction = in | out)", ["direction"])
RETRIEVAL_HITS = Histogram("rag_retrieval_hits", "Passages that survived search + rerank per question",
                           buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10))
PROVIDER_ERRORS = Counter("rag_provider_errors_total", "Failures of external services", ["kind"])

QUEUES = ("ingest", "maintenance")


class QueueDepthCollector:
    """Asks Redis how many Celery jobs are waiting in each queue (Celery keeps each queue as a Redis list)."""

    def collect(self):
        g = GaugeMetricFamily("celery_queue_length", "Jobs waiting in a Celery queue", labels=["queue"])
        try:
            import redis
            client = redis.Redis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1)
            for q in QUEUES:
                g.add_metric([q], client.llen(q))
        except Exception:  # redis down: expose nothing instead of failing the whole scrape
            pass
        yield g


def render_metrics() -> tuple[bytes, str]:
    """Returns (body, content_type) for the /metrics endpoint."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        registry.register(QueueDepthCollector())
        return generate_latest(registry), CONTENT_TYPE_LATEST
    from prometheus_client import REGISTRY
    try:
        REGISTRY.register(QueueDepthCollector())
    except ValueError:  # already registered by an earlier scrape
        pass
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
