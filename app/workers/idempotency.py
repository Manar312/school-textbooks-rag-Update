"""Idempotent tasks: running the same job twice must give the same result as running it once.

Why it matters: Celery is configured with `acks_late` (a job is only "taken off the queue" after it finishes), so if a
worker dies the job is delivered AGAIN; Beat can also re-send a job for a book that looks stuck. Without protection two
workers could ingest the same book at the same time and double the (paid / rate-limited) VLM calls.

Two protections, both stored in Redis (fast, shared by every worker):

1. PAYLOAD SIGNATURE  - sha256 of (task name + arguments). Same signature already DONE recently  -> skip.
                        Same signature currently RUNNING                                          -> skip.
2. LEASE              - a lock with a short expiry that the running task keeps renewing (a "heartbeat" thread).
                        If the worker crashes the heartbeat stops, the lease expires in a few minutes and the job
                        can be taken by someone else. A long fixed lock would block recovery for hours instead.
"""
import hashlib
import json
import logging
import threading
import uuid
from enum import Enum

log = logging.getLogger(__name__)

# Lua runs inside Redis atomically: "delete / extend the key only if it is still MY lock".
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_EXTEND = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"


class Outcome(str, Enum):
    PROCEED = "proceed"   # nobody did / is doing this job: go ahead
    RUNNING = "running"   # another worker is doing exactly this job right now
    DONE = "done"         # this exact job finished recently


class Lease:
    """A lock owned by one task. Use `with lease:` or call release()."""

    def __init__(self, client, key: str, ttl: int):
        self.client, self.key, self.ttl = client, key, ttl
        self.token = uuid.uuid4().hex   # proves the lock is mine, so I never delete somebody else's
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_heartbeat(self) -> "Lease":
        def beat():
            while not self._stop.wait(max(self.ttl / 3, 1)):
                try:
                    self.client.eval(_EXTEND, 1, self.key, self.token, self.ttl)
                except Exception:
                    log.warning("lease heartbeat failed for %s", self.key)
        self._thread = threading.Thread(target=beat, daemon=True, name=f"lease-{self.key[-8:]}")
        self._thread.start()
        return self

    def release(self) -> None:
        self._stop.set()
        try:
            self.client.eval(_RELEASE, 1, self.key, self.token)
        except Exception:
            log.warning("could not release lease %s (it will expire by itself)", self.key)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class IdempotencyManager:
    def __init__(self, client, *, prefix: str = "idem", lease_ttl: int = 300, done_ttl: int = 86400):
        self.client, self.prefix, self.lease_ttl, self.done_ttl = client, prefix, lease_ttl, done_ttl

    @staticmethod
    def signature(task_name: str, payload: dict) -> str:
        """Same task + same arguments (in any key order) -> same signature."""
        canonical = json.dumps({"task": task_name, "payload": payload}, sort_keys=True, separators=(",", ":"),
                               default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _run_key(self, sig: str) -> str:
        return f"{self.prefix}:run:{sig}"

    def _done_key(self, sig: str) -> str:
        return f"{self.prefix}:done:{sig}"

    def begin(self, sig: str) -> tuple[Outcome, Lease | None]:
        if self.client.exists(self._done_key(sig)):
            return Outcome.DONE, None
        lease = Lease(self.client, self._run_key(sig), self.lease_ttl)
        if self.client.set(lease.key, lease.token, nx=True, ex=self.lease_ttl):  # nx = only if the key is new
            return Outcome.PROCEED, lease.start_heartbeat()
        return Outcome.RUNNING, None

    def finish(self, sig: str, lease: Lease) -> None:
        """Success: remember it for `done_ttl` seconds, then give the lease back."""
        self.client.set(self._done_key(sig), "1", ex=self.done_ttl)
        lease.release()

    def abort(self, lease: Lease) -> None:
        """Failure: free the lease at once so Celery's retry is allowed to run the job again."""
        lease.release()

    def acquire(self, name: str, ttl: int | None = None) -> Lease | None:
        """A named exclusive lock (e.g. one ingestion per book). Returns None if somebody holds it."""
        lease = Lease(self.client, f"{self.prefix}:lock:{name}", ttl or self.lease_ttl)
        if self.client.set(lease.key, lease.token, nx=True, ex=lease.ttl):
            return lease.start_heartbeat()
        return None

    def is_locked(self, name: str) -> bool:
        return bool(self.client.exists(f"{self.prefix}:lock:{name}"))
