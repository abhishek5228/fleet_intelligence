"""
Tests for hybrid_search.py — specifically the tie-breaking fix in _rank_of,
which was the root cause of the "always returns DOC-01" bug.
"""
import numpy as np
import pytest
from advanced.hybrid_search import HybridSearchEngine


def _make_engine():
    docs = [
        {"id": "DOC-A", "text": "brake failure air pressure chamber pad"},
        {"id": "DOC-B", "text": "tyre burst blowout tread separation"},
        {"id": "DOC-C", "text": "engine overheating coolant radiator thermostat"},
    ]
    return HybridSearchEngine(documents=docs)


def test_rank_of_no_index_bias_on_full_tie():
    """All-zero scores must NOT always hand rank 0 to index 0 — that was the
    bug causing every unmatched query to fall back to the same document."""
    engine = _make_engine()
    scores = np.array([0.0, 0.0, 0.0])
    ranks = engine._rank_of(scores)
    # With a genuine tie, all three should get the SAME (averaged) rank —
    # not 0, 1, 2 based on array position.
    assert ranks[0] == ranks[1] == ranks[2], f"Tie-breaking bias still present: {ranks}"


def test_rank_of_orders_distinct_scores_correctly():
    engine = _make_engine()
    scores = np.array([5.0, 1.0, 3.0])
    ranks = engine._rank_of(scores)
    assert ranks[0] < ranks[2] < ranks[1]  # highest score -> lowest (best) rank


def test_genuine_keyword_match_wins():
    engine = _make_engine()
    results = engine.search("brake failure air pressure", top_k=1)
    assert results[0]["id"] == "DOC-A"


def test_no_keyword_overlap_does_not_force_doc_zero():
    """Regression test for the original bug: a query with zero real overlap
    should not deterministically always resolve to the first document."""
    engine = _make_engine()
    scores = engine._bm25_scores("completely unrelated query xyz")
    assert all(s == 0 for s in scores), "test assumption: no overlap expected"
    ranks = engine._rank_of(np.array(scores))
    assert ranks[0] == ranks[1] == ranks[2]
