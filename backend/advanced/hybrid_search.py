"""
hybrid_search.py
==================
NEW, ADDITIVE MODULE — does not modify any existing file.

Feature 1: Hybrid Search + Vector Database + BM25 + Cross-Encoder Reranking

Why: Pure dense vector search fails on exact codes like "TATA-9942-OH" or
error codes because embeddings blur exact-token precision. BM25 (sparse,
keyword-exact) fixes that. We combine both (Reciprocal Rank Fusion) and then
re-score the merged top-N with a cross-encoder reranker (Cohere Rerank or a
local BGE-Reranker) for final precision.

Usage (does not require touching main.py to test):
    from advanced.hybrid_search import HybridSearchEngine

    engine = HybridSearchEngine(documents=[
        {"id": "doc1", "text": "TATA-9942-OH oil filter housing replacement..."},
        ...
    ])
    results = engine.search("TATA-9942-OH leaking gasket", top_k=5)

Environment variables (optional — module degrades gracefully without them):
    COHERE_API_KEY   -> enables Cohere Rerank (best quality, hosted)
    USE_LOCAL_RERANKER=1 -> uses local BGE-Reranker (sentence-transformers) instead

Dependencies (see requirements-advanced.txt):
    rank-bm25
    sentence-transformers   (for dense embeddings + optional local reranker)
    cohere                  (only if using Cohere Rerank)
    numpy
"""

import os
import re
from typing import List, Dict, Any, Optional

import numpy as np
from rank_bm25 import BM25Okapi


def _tokenize(text: str) -> List[str]:
    """Simple, fast tokenizer good enough for BM25 over part numbers / codes.
    Keeps alphanumeric codes like 'TATA-9942-OH' intact as well as split forms,
    so both 'tata 9942 oh' and 'TATA-9942-OH' style queries hit."""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text)
    # also add the raw hyphenated tokens split into parts, to catch partial matches
    expanded = list(tokens)
    for t in tokens:
        if "-" in t:
            expanded.extend(t.split("-"))
    return expanded


class HybridSearchEngine:
    def __init__(
        self,
        documents: List[Dict[str, Any]],
        embedding_model_name: str = "BAAI/bge-small-en-v1.5",
    ):
        """
        documents: list of {"id": str, "text": str, **any_metadata}
        """
        self.documents = documents
        self.texts = [d["text"] for d in documents]

        # ---- Sparse index (BM25) ----
        self._tokenized_corpus = [_tokenize(t) for t in self.texts]
        self.bm25 = BM25Okapi(self._tokenized_corpus)

        # ---- Dense index (vector embeddings) ----
        self._embedder = None
        self._doc_embeddings = None
        self._embedding_model_name = embedding_model_name
        self._init_dense_index()

        # ---- Reranker ----
        self._reranker = None
        self._use_cohere = bool(os.getenv("COHERE_API_KEY")) and not os.getenv(
            "USE_LOCAL_RERANKER"
        )

    def _init_dense_index(self):
        try:
            from sentence_transformers import SentenceTransformer

            self._embedder = SentenceTransformer(self._embedding_model_name)
            self._doc_embeddings = self._embedder.encode(
                self.texts, normalize_embeddings=True
            )
        except Exception as e:
            # Degrade gracefully: hybrid falls back to BM25-only if embedding
            # model can't load (e.g. no internet / package missing at runtime).
            print(f"[hybrid_search] WARNING: dense index disabled ({e}). "
                  f"Falling back to BM25-only search.")
            self._embedder = None
            self._doc_embeddings = None

    def _dense_scores(self, query: str) -> np.ndarray:
        if self._embedder is None:
            return np.zeros(len(self.texts))
        q_emb = self._embedder.encode([query], normalize_embeddings=True)[0]
        return np.dot(self._doc_embeddings, q_emb)

    def _bm25_scores(self, query: str) -> np.ndarray:
        return np.array(self.bm25.get_scores(_tokenize(query)))

    @staticmethod
    def _rank_of(scores: np.ndarray) -> Dict[int, float]:
        """Tie-aware ranking. Plain np.argsort() breaks ties by array index —
        on a small knowledge base, every query with NO real keyword/embedding
        overlap makes all documents score exactly 0, and a naive argsort then
        always hands document index 0 the top rank regardless of relevance.
        Equal scores here get the same (averaged) rank instead, so a real tie
        no longer systematically favors whichever document happens to be
        first in the list."""
        order = np.argsort(-scores)
        sorted_scores = scores[order]
        ranks = np.empty(len(scores))
        n = len(scores)
        i = 0
        while i < n:
            j = i
            while j < n and sorted_scores[j] == sorted_scores[i]:
                j += 1
            avg_rank = (i + j - 1) / 2.0
            for m in range(i, j):
                ranks[order[m]] = avg_rank
            i = j
        return {idx: ranks[idx] for idx in range(n)}

    def _reciprocal_rank_fusion(
        self, dense_scores: np.ndarray, sparse_scores: np.ndarray, k: int = 60
    ) -> np.ndarray:
        """Combines two ranked lists without needing score normalization —
        the standard, robust way to merge dense + sparse retrieval (used by
        Elastic, Weaviate, Vespa hybrid search)."""
        dense_ranks = self._rank_of(dense_scores)
        sparse_ranks = self._rank_of(sparse_scores)
        fused = np.zeros(len(self.texts))
        for i in range(len(self.texts)):
            fused[i] = 1.0 / (k + dense_ranks[i] + 1) + 1.0 / (k + sparse_ranks[i] + 1)
        return fused

    def _rerank_cohere(self, query: str, candidates: List[Dict[str, Any]], top_k: int):
        import cohere

        co = cohere.Client(os.getenv("COHERE_API_KEY"))
        resp = co.rerank(
            model="rerank-english-v3.0",
            query=query,
            documents=[c["text"] for c in candidates],
            top_n=top_k,
        )
        reranked = []
        for r in resp.results:
            doc = candidates[r.index]
            doc = {**doc, "rerank_score": r.relevance_score}
            reranked.append(doc)
        return reranked

    def _rerank_local_bge(self, query: str, candidates: List[Dict[str, Any]], top_k: int):
        from sentence_transformers import CrossEncoder

        if self._reranker is None:
            self._reranker = CrossEncoder("BAAI/bge-reranker-base")
        pairs = [(query, c["text"]) for c in candidates]
        scores = self._reranker.predict(pairs)
        order = np.argsort(-np.array(scores))[:top_k]
        return [
            {**candidates[i], "rerank_score": float(scores[i])} for i in order
        ]

    def search(
        self, query: str, top_k: int = 5, candidate_pool: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Full pipeline: BM25 + Dense -> RRF fusion -> top `candidate_pool`
        -> Cross-Encoder Rerank -> final top_k.
        """
        sparse_scores = self._bm25_scores(query)

        if self._embedder is not None:
            dense_scores = self._dense_scores(query)
            fused_scores = self._reciprocal_rank_fusion(dense_scores, sparse_scores)
        else:
            # No dense index available -> fusing with an all-zero dense
            # channel would create spurious ties in Reciprocal Rank Fusion
            # (every doc "ties" on the meaningless dense rank), which can
            # override genuine BM25 signal on small corpora. Use BM25 alone.
            fused_scores = sparse_scores

        pool_size = min(candidate_pool, len(self.documents))
        top_idx = np.argsort(-fused_scores)[:pool_size]
        candidates = [
            {**self.documents[i], "fused_score": float(fused_scores[i])}
            for i in top_idx
        ]

        if not candidates:
            return []

        try:
            if self._use_cohere:
                return self._rerank_cohere(query, candidates, top_k)
            else:
                return self._rerank_local_bge(query, candidates, top_k)
        except Exception as e:
            # If reranker isn't available (no key / package missing),
            # fall back to the fused hybrid ranking so search still works.
            print(f"[hybrid_search] WARNING: reranker unavailable ({e}). "
                  f"Returning fused hybrid ranking without rerank.")
            return candidates[:top_k]
