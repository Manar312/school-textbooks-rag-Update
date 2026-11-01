import asyncio
from itertools import zip_longest

from app.core.config import settings
from app.providers import EmbeddingProvider, Hit, RerankerProvider, VectorStoreProvider


def diversify(hits: list[Hit], subjects: list[str], k: int) -> list[Hit]:
    """Best hit of every routed subject first (so a cross-subject question sees all of them), then fill by score."""
    hits = sorted(hits, key=lambda h: h.score, reverse=True)
    chosen: list[Hit] = []
    for s in subjects:
        top = next((h for h in hits if h.subject == s), None)
        if top is not None:
            chosen.append(top)
    chosen = chosen[:k]
    taken = {id(h) for h in chosen}
    for h in hits:
        if len(chosen) >= k:
            break
        if id(h) not in taken:
            chosen.append(h)
            taken.add(id(h))
    return sorted(chosen, key=lambda h: h.score, reverse=True)


class Retriever:
    """embed query -> per-subject search (multi-RAG fan-out) filtered by grade -> ONE cross-subject rerank + threshold.

    Each subject behaves as its own RAG (own filtered search); reranking the merged candidates together lets a
    cross-encoder compare passages from different subjects fairly."""

    def __init__(self, embedder: EmbeddingProvider, vectors: VectorStoreProvider, reranker: RerankerProvider):
        self.embedder, self.vectors, self.reranker = embedder, vectors, reranker

    async def retrieve(self, query: str, grade: str, subjects: list[str] | None = None,
                       book_id: str | None = None) -> list[Hit]:
        emb = await self.embedder.embed_query(query)
        multi = bool(subjects) and len(subjects) > 1
        if multi:
            groups = await asyncio.gather(*(
                self.vectors.search(emb, grade=grade, subject=s, book_id=book_id, limit=settings.rerank_per_subject)
                for s in subjects))
            hits = [h for g in groups for h in g]
        else:
            hits = await self.vectors.search(emb, grade=grade, subject=subjects[0] if subjects else None,
                                             book_id=book_id, limit=settings.top_k_candidates)
        if not hits:
            return []
        scores = await self.reranker.rerank(query, [h.text for h in hits])
        if scores is not None:
            for h, s in zip(hits, scores):
                h.score = s
            hits = [h for h in hits if h.score >= settings.rerank_min_score]
        elif multi:  # no reranker: interleave the per-subject rankings so no subject is starved
            hits = [h for row in zip_longest(*groups) for h in row if h is not None]
        if not hits:
            return []
        if multi and scores is not None:
            return diversify(hits, subjects, settings.top_k_final)
        if scores is not None:
            hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:settings.top_k_final]
