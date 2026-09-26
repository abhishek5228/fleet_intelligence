"""
self_rag.py
============
NEW, ADDITIVE MODULE — does not modify any existing file.

Feature 3: Self-RAG + Corrective RAG (CRAG) + Hallucination Grader.

Three guardrails, chained together:

  1. RelevanceGrader   -> Before answering, an LLM call checks: "is the
                           retrieved context actually relevant to this
                           query?" If not -> trigger a corrective action
                           (widen search / web fallback) instead of answering
                           from bad context.

  2. CorrectiveFallback -> If context is irrelevant/insufficient, this is
                           where you'd plug a web search or a wider hybrid
                           search re-query (kept as a pluggable callback so
                           you can wire your existing search / web tools in).

  3. HallucinationGrader -> After the LLM drafts an answer, checks that any
                           part number / code mentioned (e.g. "TATA-9942-OH")
                           actually appears in the retrieved context. If the
                           LLM invented a part number that isn't grounded in
                           the source documents, the answer is rejected/flagged.

This uses Gemini (to match your existing GEMINI_API_KEY usage in main.py) for
the grading calls, via plain REST — no new SDK dependency required.
"""

import os
import re
import json
from typing import List, Dict, Any, Callable, Optional

import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_GRADER_MODEL", "gemini-2.0-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def _call_gemini_json(system_instruction: str, user_content: str) -> dict:
    """Calls Gemini and expects a strict JSON response back. Falls back to a
    safe default if the API key is missing or the call fails, so this never
    crashes the pipeline — it just skips the extra guardrail."""
    if not GEMINI_API_KEY:
        return {"_unavailable": True}

    body = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as e:
        print(f"[self_rag] WARNING: grader call failed ({e}). Skipping this guardrail.")
        return {"_unavailable": True}


# ---------------------------------------------------------------------------
# 1. Relevance Grader (Self-RAG)
# ---------------------------------------------------------------------------
def grade_relevance(query: str, retrieved_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Returns: {"relevant": bool, "reason": str, "scores": {doc_id: bool}}
    """
    if not retrieved_docs:
        return {"relevant": False, "reason": "No documents retrieved.", "scores": {}}

    context_block = "\n\n".join(
        f"[{d.get('id', i)}] {d.get('text', '')}" for i, d in enumerate(retrieved_docs)
    )
    system = (
        "You are a strict relevance grader for a truck-fleet RAG system. "
        "Given a query and retrieved documents, decide if the documents "
        "contain information that actually answers the query "
        "(e.g. correct part number, matching error code, matching truck model). "
        "Respond ONLY with JSON: "
        '{"relevant": true/false, "reason": "...", "per_doc": {"<doc_id>": true/false}}'
    )
    user = f"QUERY: {query}\n\nRETRIEVED DOCUMENTS:\n{context_block}"

    result = _call_gemini_json(system, user)
    if result.get("_unavailable"):
        # Fail open but flagged: treat as relevant so pipeline still works,
        # but caller can check '_graded' to know this wasn't actually graded.
        return {"relevant": True, "reason": "Grader unavailable.", "_graded": False}

    return {
        "relevant": bool(result.get("relevant", False)),
        "reason": result.get("reason", ""),
        "per_doc": result.get("per_doc", {}),
        "_graded": True,
    }


# ---------------------------------------------------------------------------
# 2. Corrective fallback (CRAG)
# ---------------------------------------------------------------------------
def corrective_retrieve(
    query: str,
    fallback_search_fn: Callable[[str], List[Dict[str, Any]]],
    widened_query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    fallback_search_fn: any callable(query) -> List[docs]. Wire your
    HybridSearchEngine.search with a widened/rephrased query here, or a
    real web-search tool, or both in sequence.
    """
    query_to_use = widened_query or f"{query} (broader truck maintenance context)"
    return fallback_search_fn(query_to_use)


# ---------------------------------------------------------------------------
# 3. Hallucination Grader — grounds part numbers / codes against source docs
# ---------------------------------------------------------------------------
_CODE_PATTERN = re.compile(r"\b[A-Z]{2,}-?\d{2,}[A-Z0-9-]*\b")


def extract_codes(text: str) -> List[str]:
    """Pulls things that look like part numbers / error codes, e.g. TATA-9942-OH."""
    return list(set(_CODE_PATTERN.findall(text.upper())))


def grade_hallucination(
    generated_answer: str, retrieved_docs: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Deterministic first pass (fast, free, no LLM call): every code-like token
    mentioned in the answer MUST appear verbatim in at least one retrieved
    document. If not -> flagged as a likely hallucinated part number.

    Returns: {"grounded": bool, "ungrounded_codes": [...], "checked_codes": [...]}
    """
    source_text = " ".join(d.get("text", "") for d in retrieved_docs).upper()
    answer_codes = extract_codes(generated_answer)

    ungrounded = [c for c in answer_codes if c not in source_text]

    return {
        "grounded": len(ungrounded) == 0,
        "ungrounded_codes": ungrounded,
        "checked_codes": answer_codes,
    }


# ---------------------------------------------------------------------------
# Orchestration helper: run all three guardrails together
# ---------------------------------------------------------------------------
def self_rag_pipeline(
    query: str,
    retrieved_docs: List[Dict[str, Any]],
    generate_answer_fn: Callable[[str, List[Dict[str, Any]]], str],
    fallback_search_fn: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    Full Self-RAG + CRAG + hallucination-guard loop.

    generate_answer_fn(query, docs) -> str   # your existing Gemini call_ai_agent-style function
    fallback_search_fn(query) -> List[docs]  # e.g. HybridSearchEngine.search
    """
    relevance = grade_relevance(query, retrieved_docs)

    docs_to_use = retrieved_docs
    if not relevance["relevant"] and fallback_search_fn is not None:
        docs_to_use = corrective_retrieve(query, fallback_search_fn)
        relevance = grade_relevance(query, docs_to_use)  # re-grade after correction

    answer = generate_answer_fn(query, docs_to_use)
    hallucination_check = grade_hallucination(answer, docs_to_use)

    return {
        "answer": answer,
        "used_corrective_retrieval": docs_to_use is not retrieved_docs,
        "relevance": relevance,
        "hallucination_check": hallucination_check,
        "safe_to_show_user": relevance["relevant"] and hallucination_check["grounded"],
    }
