"""Answer orchestration: smalltalk shortcut -> condense follow-up -> retrieve -> stream answer with citations.
Depends only on provider interfaces (via Retriever / LLMProvider) and repositories."""
import asyncio
import logging
import uuid
from typing import AsyncIterator

from redis.asyncio import Redis

from app.core.config import settings
from app.core import metrics
from app.core.errors import AppError
from app.core.textutil import NOT_FOUND_MESSAGES, detect_language, smalltalk_reply
from app.providers import LLMProvider
from app.services.prompts import (SYSTEM_TEMPLATE, format_context, grade_label, normalize_history,
                                  used_citation_numbers)
from app.services.query_planner import QueryPlanner
from app.services.quota_service import QuotaService
from app.services.retrieval import Retriever

log = logging.getLogger(__name__)
LOCK_TTL = 120


def is_rate_limited(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 429 or type(e).__name__ == "RateLimitError"


class RagService:
    def __init__(self, *, llm: LLMProvider, planner: QueryPlanner, retriever: Retriever, quota: QuotaService,
                 open_chat_repo, redis: Redis | None):
        self.llm, self.planner, self.retriever = llm, planner, retriever
        self.quota, self.open_chat_repo, self.redis = quota, open_chat_repo, redis

    # one in-flight answer per chat: prevents interleaved history when a student double-submits
    async def acquire_lock(self, session_id: uuid.UUID) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.set(f"chatlock:{session_id}", "1", nx=True, ex=LOCK_TTL))
        except Exception:
            log.warning("redis unavailable: chat lock skipped")
            return True

    async def release_lock(self, session_id: uuid.UUID) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(f"chatlock:{session_id}")
        except Exception:
            log.warning("could not release chat lock %s (expires by TTL)", session_id)

    async def _prepare(self, user_id, session_id, question) -> list[dict]:
        async with self.open_chat_repo() as repo:
            history = await repo.recent_history(session_id, user_id, settings.history_messages)
            await repo.add_message(session_id=session_id, user_id=user_id, role="user", content=question)
            await repo.set_title_if_default(session_id, user_id, question.strip().replace("\n", " ")[:60])
        return [{"role": m["role"], "content": m["content"][:2000]} for m in history]

    async def _finalize(self, user_id, session_id, answer, citations, tin, tout) -> None:
        try:
            if answer.strip():
                async with self.open_chat_repo() as repo:
                    await repo.add_message(session_id=session_id, user_id=user_id, role="assistant", content=answer,
                                           citations=citations, tokens_in=tin, tokens_out=tout)
            if answer.strip() or tin or tout:
                await self.quota.record(user_id, tin, tout)
        except Exception:
            log.exception("finalize failed for session %s", session_id)
        finally:
            await self.release_lock(session_id)

    async def ask(self, *, user_id: uuid.UUID, grade: str, session_id: uuid.UUID, question: str,
                  subject: str | None = None, book_id: str | None = None,
                  subjects: dict[str, list[str]] | None = None) -> AsyncIterator[dict]:
        """`subjects` = {subject name: [book titles]} the student can see: the router chooses among them."""
        answer_parts: list[str] = []
        citations: list[dict] | None = None
        tin = tout = prompt_chars = 0
        usage_seen = False
        route_method, outcome = "none", "error"
        try:
            yield {"event": "meta", "data": {"session_id": str(session_id)}}
            history = await self._prepare(user_id, session_id, question)

            canned = smalltalk_reply(question)
            if canned:
                answer_parts.append(canned)
                yield {"event": "token", "data": {"text": canned}}
                yield {"event": "citations", "data": []}
                outcome = "smalltalk"
                return

            plan = await self.planner.plan(question, history, subjects or {}, explicit_subject=subject,
                                           book_pinned=bool(book_id))
            tin, tout = tin + plan.tokens_in, tout + plan.tokens_out
            route_method = plan.method
            yield {"event": "route", "data": {"method": plan.method, "subjects": plan.subjects or [],
                                              "scope": "subjects" if plan.subjects else "all", "query": plan.query}}
            try:
                hits = await self.retriever.retrieve(plan.query, grade, plan.subjects, book_id)
                if not hits and plan.subjects and plan.method in ("llm", "keyword"):
                    # the router may have guessed wrong: widen to every subject of the grade before giving up
                    hits = await self.retriever.retrieve(plan.query, grade, None, book_id)
                    yield {"event": "route", "data": {"method": "fallback_all", "subjects": [], "scope": "all",
                                                      "query": plan.query}}
            except Exception as e:
                log.exception("retrieval failed")
                metrics.PROVIDER_ERRORS.labels("retrieval").inc()
                raise AppError("Search is temporarily unavailable, please try again.", code="search_unavailable",
                               status_code=503) from e

            metrics.RETRIEVAL_HITS.observe(len(hits))
            if not hits:
                outcome = "not_found"
                msg = NOT_FOUND_MESSAGES[detect_language(question)]
                answer_parts.append(msg)
                yield {"event": "token", "data": {"text": msg}}
                yield {"event": "citations", "data": []}
                return

            context, used = format_context(hits, settings.max_context_chars)
            system = SYSTEM_TEMPLATE.format(grade_label=grade_label(grade))
            messages = normalize_history(history + [{"role": "user", "content": f"{context}\n\nStudent question: {question}"}])
            prompt_chars = len(system) + sum(len(m["content"]) for m in messages)

            try:
                async for chunk in self.llm.stream(system=system, messages=messages,
                                                   max_tokens=settings.answer_max_tokens):
                    if chunk.text:
                        answer_parts.append(chunk.text)
                        yield {"event": "token", "data": {"text": chunk.text}}
                    if chunk.tokens_in is not None:
                        tin += chunk.tokens_in
                        tout += chunk.tokens_out or 0
                        usage_seen = True
            except Exception as e:
                log.exception("llm stream failed")
                metrics.PROVIDER_ERRORS.labels("llm").inc()
                if is_rate_limited(e):  # free-tier quota of the AI vendor, shared by all students
                    raise AppError("The AI service is busy right now. Please try again in a minute.",
                                   code="provider_busy", status_code=429) from e
                raise AppError("The tutor is temporarily unavailable, please try again.", code="llm_unavailable",
                               status_code=502) from e

            answer = "".join(answer_parts)
            nums = used_citation_numbers(answer, len(used)) or list(range(1, len(used) + 1))
            citations = [{
                "n": n, "book_id": used[n - 1].book_id, "book_title": used[n - 1].book_title,
                "subject": used[n - 1].subject, "section": used[n - 1].heading,
                "pages": [used[n - 1].page_start, used[n - 1].page_end], "snippet": used[n - 1].text[:240],
            } for n in nums]
            yield {"event": "citations", "data": citations}
            outcome = "answered"
        except AppError as e:
            yield {"event": "error", "data": {"code": e.code, "message": e.message}}
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ask failed")
            yield {"event": "error", "data": {"code": "internal", "message": "Something went wrong."}}
        finally:
            if not usage_seen and answer_parts and prompt_chars:  # client disconnected mid-stream: estimate
                tin += prompt_chars // 3
                tout += len("".join(answer_parts)) // 3
            metrics.QUESTIONS.labels(route_method, outcome).inc()
            metrics.LLM_TOKENS.labels("in").inc(tin)
            metrics.LLM_TOKENS.labels("out").inc(tout)
            # shielded: must still persist the (partial) answer and release the lock on client disconnect
            await asyncio.shield(self._finalize(user_id, session_id, "".join(answer_parts), citations, tin, tout))
