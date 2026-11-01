"""Multi-RAG: planner (rewrite + subject routing), multi-subject retrieval, fallbacks, free-tier protections."""
import json
import uuid

import pytest

from app.core.config import Settings
from app.core.errors import ConfigError, RateLimited
from app.core.textutil import normalize_text
from app.providers import LLMResult, Providers
from app.providers.llm.openai_llm import usage_of
from app.services.query_planner import (QueryPlanner, keyword_subjects, match_subjects, parse_plan_json)
from app.services.quota_service import QuotaService
from app.services.retrieval import Retriever, diversify
from tests.test_architecture import (FakeEmbedder, FakeLLM, FakeReranker, FakeVectors, collect, hit, make_rag)

SUBJECTS = {"physics": ["Physics T1"], "math": ["Math T1"], "الأحياء": ["Biology T1"]}


class PlanLLM(FakeLLM):
    """LLM that answers the planner prompt with a canned JSON string."""

    def __init__(self, reply_json):
        super().__init__()
        self.reply_json = reply_json

    async def complete(self, *, system, messages, max_tokens=1024):
        self.calls.append(("complete", messages))
        if isinstance(self.reply_json, Exception):
            raise self.reply_json
        return LLMResult(self.reply_json, 40, 12)


# ------------------------------------------------------------------ pure helpers
def test_parse_plan_json_tolerates_fences_and_chatter():
    assert parse_plan_json('```json\n{"query": "q", "subjects": ["math"]}\n```')["query"] == "q"
    assert parse_plan_json('Sure! {"query": "q"} hope it helps')["query"] == "q"
    assert parse_plan_json("no json here") is None
    assert parse_plan_json("{broken") is None
    assert parse_plan_json("[1, 2]") is None


def test_match_subjects_drops_invented_names_and_normalizes_arabic():
    names = ["physics", "الأحياء"]
    assert match_subjects(["Physics", "chemistry"], names) == ["physics"]
    assert match_subjects(["الاحياء"], names) == ["الأحياء"]          # alef variant
    assert match_subjects("physics", names) == []                    # not a list
    assert match_subjects(["physics", "physics"], names) == ["physics"]


def test_keyword_subjects_with_and_without_al_prefix():
    assert keyword_subjects("اشرح لي درس في الاحياء", ["الأحياء", "math"]) == ["الأحياء"]
    assert keyword_subjects("ما هي الأحياء الدقيقة؟", ["احياء"]) == ["احياء"]
    assert keyword_subjects("solve this math problem", ["physics", "math"]) == ["math"]
    assert keyword_subjects("hello there", ["physics", "math"]) == []


def test_normalize_text():
    assert normalize_text("الأَحْيَاءُ") == normalize_text("الاحياء")
    assert normalize_text("مدرسة") == normalize_text("مدرسه")


# ------------------------------------------------------------------ planner
async def test_explicit_subject_skips_the_llm():
    llm = PlanLLM('{"query": "x"}')
    plan = await QueryPlanner(llm).plan("q?", [], SUBJECTS, explicit_subject="math")
    assert plan.subjects == ["math"] and plan.method == "explicit" and llm.calls == []


async def test_single_subject_and_keyword_paths_need_no_llm():
    llm = PlanLLM('{"query": "x"}')
    assert (await QueryPlanner(llm).plan("q?", [], {"math": ["M"]})).method == "single"
    plan = await QueryPlanner(llm).plan("a physics question", [], SUBJECTS)
    assert plan.subjects == ["physics"] and plan.method == "keyword" and llm.calls == []


async def test_llm_routes_to_valid_subjects_and_counts_tokens():
    llm = PlanLLM(json.dumps({"query": "newton second law", "subjects": ["physics", "fake"], "confidence": 0.9}))
    plan = await QueryPlanner(llm).plan("what makes things accelerate?", [], SUBJECTS)
    assert plan.subjects == ["physics"] and plan.method == "llm" and plan.tokens_in == 40


@pytest.mark.parametrize("reply", [
    '{"query": "q", "subjects": ["physics"], "confidence": 0.1}',   # unsure
    '{"query": "q", "subjects": ["nonsense"], "confidence": 0.9}',  # invented subject
    '{"query": "q", "subjects": [], "confidence": 0.9}',            # empty
    '{"query": "q", "subjects": ["physics"], "confidence": "high"}',  # garbage confidence
    'I cannot answer',                                              # not JSON
])
async def test_bad_or_unsure_router_output_falls_back_to_all_subjects(reply):
    plan = await QueryPlanner(PlanLLM(reply)).plan("ambiguous question", [], SUBJECTS)
    assert plan.subjects is None and plan.method == "all"


async def test_planner_llm_failure_never_breaks_the_question():
    plan = await QueryPlanner(PlanLLM(RuntimeError("down"))).plan("some question", [], SUBJECTS)
    assert plan.query == "some question" and plan.subjects is None


async def test_followup_is_rewritten_using_history():
    llm = PlanLLM('{"query": "importance of photosynthesis", "subjects": ["math"], "confidence": 0.9}')
    history = [{"role": "user", "content": "what is photosynthesis?"}, {"role": "assistant", "content": "..."}]
    plan = await QueryPlanner(llm).plan("and why is it important?", history, SUBJECTS, explicit_subject="physics")
    assert plan.query == "importance of photosynthesis"
    assert plan.subjects == ["physics"]          # explicit choice is never overridden by the router


# ------------------------------------------------------------------ multi-subject retrieval
def multi_hits():
    return [hit(subject="physics", key="p1", text="force and acceleration"),
            hit(subject="physics", key="p2", text="newton laws"),
            hit(subject="math", key="m1", text="derivative of a function"),
            hit(subject="الأحياء", key="b1", text="cells")]


async def test_fan_out_searches_each_routed_subject_separately():
    v = FakeVectors(multi_hits())
    await Retriever(FakeEmbedder(), v, FakeReranker([0.9, 0.8, 0.7])).retrieve("q", "prep-1", ["physics", "math"])
    assert sorted(c[1] for c in v.calls) == ["math", "physics"]


async def test_cross_subject_answer_contains_every_routed_subject():
    v = FakeVectors(multi_hits())
    r = Retriever(FakeEmbedder(), v, FakeReranker([0.99, 0.98, 0.4]))   # physics hits outrank math
    hits = await r.retrieve("compare", "prep-1", ["physics", "math"])
    assert {h.subject for h in hits} == {"physics", "math"}            # diversity guarantee


def test_diversify_keeps_best_of_each_subject_first():
    hs = [hit(subject="a", key="1"), hit(subject="a", key="2"), hit(subject="b", key="3")]
    for h, s in zip(hs, (0.9, 0.8, 0.1)):
        h.score = s
    out = diversify(hs, ["a", "b"], 2)
    assert {h.subject for h in out} == {"a", "b"} and len(out) == 2


async def test_no_reranker_interleaves_subjects():
    from app.providers.rerankers.noop import NoopReranker
    v = FakeVectors(multi_hits())
    hits = await Retriever(FakeEmbedder(), v, NoopReranker("none")).retrieve("q", "prep-1", ["physics", "math"])
    assert [h.subject for h in hits][:2] == ["physics", "math"]


async def test_single_or_no_subject_uses_one_search():
    v = FakeVectors(multi_hits())
    r = Retriever(FakeEmbedder(), v, FakeReranker([0.9] * 4))
    await r.retrieve("q", "prep-1", None)
    await r.retrieve("q", "prep-1", ["math"])
    assert [c[1] for c in v.calls] == [None, "math"]


# ------------------------------------------------------------------ end-to-end RAG with routing
async def test_rag_emits_route_event_and_searches_only_the_routed_subject():
    llm = PlanLLM('{"query": "force", "subjects": ["physics"], "confidence": 0.9}')
    rag, repo, quota, _ = make_rag(llm=llm, hits=multi_hits())
    llm.reply = "القوة تساوي الكتلة في العجلة [1]."
    events = await collect(rag, "what is force?", subjects=SUBJECTS)
    route = next(e for e in events if e["event"] == "route")["data"]
    assert route["method"] == "llm" and route["subjects"] == ["physics"] and route["scope"] == "subjects"
    assert {c[1] for c in rag.vectors.calls} == {"physics"}
    assert quota.recorded[0][0] >= 40            # planner tokens are billed to the student


async def test_wrong_route_widens_to_all_subjects_before_giving_up():
    llm = PlanLLM('{"query": "cells", "subjects": ["math"], "confidence": 0.9}')
    rag, *_ = make_rag(llm=llm, hits=[hit(subject="الأحياء", key="b1")])   # answer lives in biology only
    events = await collect(rag, "what are cells?", subjects=SUBJECTS)
    routes = [e["data"]["method"] for e in events if e["event"] == "route"]
    assert routes == ["llm", "fallback_all"]
    assert events[-1]["event"] == "citations" and events[-1]["data"]       # found after widening


async def test_free_tier_rate_limit_becomes_a_friendly_429_event():
    class RateLimitError(Exception):
        status_code = 429

    class Busy(FakeLLM):
        async def stream(self, **kw):
            raise RateLimitError("rate limit reached")
            yield
    rag, *_ = make_rag(llm=Busy())
    events = await collect(rag)
    assert events[-1]["data"]["code"] == "provider_busy"


# ------------------------------------------------------------------ global limiter
class FakeRedis:
    def __init__(self):
        self.n = {}

    async def incr(self, k):
        self.n[k] = self.n.get(k, 0) + 1
        return self.n[k]

    async def expire(self, *a):
        return True


async def test_global_question_limit_protects_the_shared_free_quota(monkeypatch):
    from contextlib import asynccontextmanager

    class Repo:
        async def get(self, *a):
            return None

    @asynccontextmanager
    async def open_repo():
        yield Repo()
    from app.core import config
    monkeypatch.setattr(config.settings, "global_questions_per_minute", 2)
    monkeypatch.setattr(config.settings, "rate_limit_per_minute", 100)
    q = QuotaService(open_repo, FakeRedis())
    await q.enforce(uuid.uuid4(), 10, 1000)
    await q.enforce(uuid.uuid4(), 10, 1000)
    with pytest.raises(RateLimited, match="busy"):
        await q.enforce(uuid.uuid4(), 10, 1000)


# ------------------------------------------------------------------ free providers
def test_defaults_are_free_groq_models_with_local_embeddings():
    s = Settings(jwt_secret="x")
    assert (s.llm_provider, s.vlm_provider, s.condense_provider) == ("groq", "groq", "groq")
    assert s.embed_provider == "bge_m3" and s.rerank_provider == "bge" and s.ocr_provider == "tesseract"
    assert s.vlm_concurrency == 1


def test_groq_and_gemini_need_their_own_key_and_use_the_right_endpoint():
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        Providers(Settings(jwt_secret="x", groq_api_key="")).llm
    p = Providers(Settings(jwt_secret="x", groq_api_key="gk", gemini_api_key="mk", llm_provider="gemini",
                           llm_model="gemini-flash-latest"))
    assert p.llm.name == "gemini" and "generativelanguage" in str(p.llm.client.base_url)
    q = Providers(Settings(jwt_secret="x", groq_api_key="gk"))
    assert q.llm.name == "groq" and "api.groq.com" in str(q.llm.client.base_url)
    assert q.vlm.name == "groq" and q.condense_llm.model == "llama-3.1-8b-instant"


def test_usage_of_reads_standard_and_groq_extension():
    class U:
        prompt_tokens, completion_tokens = 7, 3

    class Std:
        usage = U()

    class Groq:
        usage = None
        model_extra = {"x_groq": {"usage": {"prompt_tokens": 11, "completion_tokens": 5}}}

    class Empty:
        usage = None
        model_extra = {}
    assert usage_of(Std()) == (7, 3) and usage_of(Groq()) == (11, 5) and usage_of(Empty()) is None
