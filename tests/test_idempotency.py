"""Idempotent task manager: duplicate / crashed / racing jobs. Uses fakeredis (a real Redis protocol engine in memory)."""
import time

import fakeredis
import pytest

from app.workers.idempotency import IdempotencyManager, Lease, Outcome
from app.workers.tasks import run_idempotent


@pytest.fixture
def client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def mgr(client):
    return IdempotencyManager(client, lease_ttl=30, done_ttl=60)


class RetryRequested(Exception):
    pass


class FakeTask:
    """Stands in for the bound Celery task: `retry()` raises like the real one does."""

    def __init__(self):
        self.retries = []

    def retry(self, **kw):
        self.retries.append(kw)
        return RetryRequested(kw)


# ---------------------------------------------------------------- signature
def test_signature_ignores_key_order_but_not_values_or_task_name():
    a = IdempotencyManager.signature("t", {"book": "1", "req": "a"})
    assert a == IdempotencyManager.signature("t", {"req": "a", "book": "1"})
    assert a != IdempotencyManager.signature("t", {"book": "1", "req": "b"})
    assert a != IdempotencyManager.signature("other", {"book": "1", "req": "a"})


# ---------------------------------------------------------------- begin / finish / abort
def test_second_begin_while_running_is_refused_and_after_finish_it_is_done(mgr):
    sig = "s1"
    outcome, lease = mgr.begin(sig)
    assert outcome is Outcome.PROCEED and isinstance(lease, Lease)
    assert mgr.begin(sig) == (Outcome.RUNNING, None)
    mgr.finish(sig, lease)
    assert mgr.begin(sig) == (Outcome.DONE, None)


def test_abort_frees_the_job_so_a_retry_can_run(mgr):
    outcome, lease = mgr.begin("s2")
    mgr.abort(lease)
    assert mgr.begin("s2")[0] is Outcome.PROCEED


def test_a_lease_never_releases_someone_elses_lock(client, mgr):
    mine = mgr.acquire("book:1")
    thief = Lease(client, mine.key, 30)           # different token, same key
    thief.release()
    assert mgr.is_locked("book:1")                # still locked: the thief's release was ignored
    mine.release()
    assert not mgr.is_locked("book:1")


def test_named_lock_is_exclusive(mgr):
    first = mgr.acquire("book:9")
    assert first is not None and mgr.acquire("book:9") is None
    first.release()
    assert mgr.acquire("book:9") is not None


def test_heartbeat_keeps_a_live_lease_and_a_dead_one_expires(client):
    m = IdempotencyManager(client, lease_ttl=2)
    alive = m.acquire("alive")
    dead = Lease(client, "idem:lock:dead", 2)                    # same key shape, but NO heartbeat (crashed worker)
    client.set(dead.key, dead.token, ex=2)
    time.sleep(3.2)
    assert m.is_locked("alive")        # heartbeat renewed it
    assert not m.is_locked("dead")     # nobody renewed it: expired, so another worker can take over
    alive.release()


# ---------------------------------------------------------------- run_idempotent (what every Celery task goes through)
def test_work_runs_once_and_a_duplicate_message_is_skipped(mgr):
    calls, task = [], FakeTask()
    work = lambda: calls.append(1) or {"status": "ok"}  # noqa: E731
    assert run_idempotent(task, "ingest.extract", "b1", "r1", work, mgr) == {"status": "ok"}
    again = run_idempotent(task, "ingest.extract", "b1", "r1", work, mgr)   # same request re-delivered
    assert again["status"] == "skipped" and again["reason"] == "already done" and len(calls) == 1


def test_a_new_request_for_the_same_book_runs_again(mgr):
    calls, task = [], FakeTask()
    work = lambda: calls.append(1) or {}  # noqa: E731
    run_idempotent(task, "ingest.index", "b1", "r1", work, mgr)
    run_idempotent(task, "ingest.index", "b1", "r2", work, mgr)             # admin clicked reindex again
    assert len(calls) == 2


def test_same_request_running_elsewhere_is_skipped(mgr):
    outcome, lease = mgr.begin(mgr.signature("ingest.extract", {"book_id": "b1", "request_id": "r1"}))
    res = run_idempotent(FakeTask(), "ingest.extract", "b1", "r1", lambda: pytest.fail("must not run"), mgr)
    assert res["reason"] == "already running"
    lease.release()


def test_failure_releases_everything_and_asks_celery_to_retry(mgr):
    task = FakeTask()

    def boom():
        raise ValueError("vlm exploded")
    with pytest.raises(RetryRequested):
        run_idempotent(task, "ingest.extract", "b1", "r1", boom, mgr)
    assert task.retries and not mgr.is_locked("book:b1")
    # the retry can now succeed because nothing is left locked
    assert run_idempotent(task, "ingest.extract", "b1", "r1", lambda: {"status": "ok"}, mgr) == {"status": "ok"}


def test_two_requests_for_one_book_never_run_at_the_same_time(mgr):
    task = FakeTask()
    inner = {}

    def first_request_work():
        # while request 1 is working, request 2 arrives for the SAME book
        with pytest.raises(RetryRequested):
            run_idempotent(task, "ingest.extract", "b1", "r2", lambda: pytest.fail("must wait"), mgr)
        inner["retry"] = task.retries[-1]
        return {"status": "ok"}
    assert run_idempotent(task, "ingest.extract", "b1", "r1", first_request_work, mgr) == {"status": "ok"}
    assert inner["retry"]["countdown"] == 120                     # waits 2 minutes, then tries again
    assert not mgr.begin(mgr.signature("ingest.extract", {"book_id": "b1", "request_id": "r2"}))[0] is Outcome.DONE
