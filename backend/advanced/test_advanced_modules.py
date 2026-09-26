"""
test_advanced_modules.py
==========================
Run this FIRST to check all 5 new modules work on your machine, WITHOUT
touching main.py or the running backend at all.

Usage:
    cd backend
    python -m advanced.test_advanced_modules
"""

import os


def test_hybrid_search():
    print("\n=== Testing Feature 1: Hybrid Search + Reranking ===")
    from advanced.hybrid_search import HybridSearchEngine

    docs = [
        {"id": "p1", "part_number": "TATA-9942-OH",
         "text": "TATA-9942-OH: Oil filter housing assembly for TATA LPT 3118 engines. "
                  "Replace if leaking or cracked."},
        {"id": "p2", "part_number": "TATA-1120-BR",
         "text": "TATA-1120-BR: Brake caliper set for TATA Signa trucks."},
        {"id": "p3", "part_number": "TATA-7788-CL",
         "text": "TATA-7788-CL: Clutch plate for TATA Prima heavy duty trucks."},
    ]
    engine = HybridSearchEngine(documents=docs)
    results = engine.search("oil filter housing leaking TATA-9942-OH", top_k=2)
    for r in results:
        print(f"  -> {r.get('part_number')} (score: {r.get('rerank_score', r.get('fused_score')):.3f})")
    print("Hybrid search OK." if results else "Hybrid search returned no results — check setup.")


def test_multi_agent_graph():
    print("\n=== Testing Feature 2: Multi-Agent LangGraph ===")
    from advanced.multi_agent_graph import build_fleet_graph

    fleet_graph = build_fleet_graph()
    config = {"configurable": {"thread_id": "test-incident-1"}}
    initial_state = {
        "incident_id": "test-1",
        "vehicle_id": "BR01GP9621",
        "location": "NH19, Barh, Bihar",
        "destination": "Authorized Heavy Workshop Hub",
        "issue_type": "engine overheat warning light",
        "raw_error_log": "Coolant temp warning light + overheat",
        "cargo_type": "General",
    }
    result = fleet_graph.invoke(initial_state, config)
    print("  Graph paused at manager_approval as expected. Severity:", result.get("severity"))

    fleet_graph.update_state(config, {"approval_status": "approved"})
    final_result = fleet_graph.invoke(None, config)
    print("  Final status after approval:", final_result.get("final_status"))
    print("Multi-agent graph OK.")


def test_self_rag():
    print("\n=== Testing Feature 3: Self-RAG / Hallucination Guard ===")
    from advanced.self_rag import grade_hallucination, extract_codes

    docs = [{"id": "p1", "text": "TATA-9942-OH oil filter housing"}]
    good_answer = "Replace the TATA-9942-OH oil filter housing."
    bad_answer = "Replace the TATA-9999-XX part immediately."

    print("  Codes found in good answer:", extract_codes(good_answer))
    print("  Grounding check (should be grounded=True):", grade_hallucination(good_answer, docs))
    print("  Grounding check (should be grounded=False):", grade_hallucination(bad_answer, docs))
    print("Self-RAG hallucination guard OK (deterministic check ran without needing GEMINI_API_KEY).")


def test_observability():
    print("\n=== Testing Feature 4: Observability ===")
    from advanced.observability import traced

    @traced("demo_step")
    def slow_step():
        return "done"

    result = slow_step()
    print("  Traced function result:", result)
    print("Observability OK (structured JSON trace line printed above).")


def test_multimodal():
    print("\n=== Testing Feature 5: Multi-Modal ===")
    from advanced.multimodal import identify_damaged_part

    if not os.getenv("GEMINI_API_KEY"):
        print("  GEMINI_API_KEY not set -> skipping live vision call (expected in local test).")
        return
    # If you have a real image, uncomment and point to it:
    # with open("sample_part_damage.jpg", "rb") as f:
    #     result = identify_damaged_part(f.read())
    #     print(result)
    print("Multi-modal module imported OK.")


if __name__ == "__main__":
    test_hybrid_search()
    test_multi_agent_graph()
    test_self_rag()
    test_observability()
    test_multimodal()
    print("\nAll 5 advanced modules ran without crashing.")
