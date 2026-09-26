# Advanced Modules — Integration Guide

**Kuch bhi apne aap change nahi hua hai.** `backend/main.py`, `frontend/*`,
`requirements.txt` — sab bilkul original hain, ek line bhi nahi chhedi gayi.

Yeh naya folder `backend/advanced/` mein 5 independent modules hain. Aap in
files ko sirf **copy karke apne repo mein daal do** — kuch bhi automatically
wire nahi hota jab tak aap khud import na karo. Isliye zero risk hai ki
kuch existing tootega.

```
backend/advanced/
├── __init__.py
├── hybrid_search.py          # Feature 1
├── multi_agent_graph.py      # Feature 2
├── self_rag.py                # Feature 3
├── observability.py           # Feature 4
├── multimodal.py               # Feature 5
└── requirements-advanced.txt
```

## Step 1 — Add these files to your real repo

```bash
git clone https://github.com/projects2829/fleet-swarm-project.git
cd fleet-swarm-project
# copy the backend/advanced/ folder you downloaded from Claude into here
git add backend/advanced/
git commit -m "Add advanced RAG/agentic modules (hybrid search, multi-agent, self-RAG, observability, multimodal) — additive only"
git push
```

## Step 2 — Install new dependencies (separate file, doesn't touch requirements.txt)

```bash
pip install -r backend/advanced/requirements-advanced.txt
```

This installs only the CORE, always-safe packages (`rank-bm25`, `numpy`,
`langgraph`, `langchain-core`) with no strict version pins, so it will never
hit a `ResolutionImpossible` conflict on Render.

Heavy/optional packages (`sentence-transformers` for local reranking,
`cohere` for hosted reranking, `langsmith`/`opentelemetry` for tracing) live
in a **separate** `requirements-advanced-optional.txt`, commented out by
default. Uncomment and install **one at a time**, only when you're ready to
use that specific feature — never add the whole optional file to your build
command in one go, since mixing several optional extras together is what
caused the earlier `ResolutionImpossible` error with `langsmith`.

**On Render specifically:** update your service's **Build Command** to:
```
pip install -r requirements.txt -r advanced/requirements-advanced.txt
```
(adjust the path if your `requirements.txt` isn't directly under `backend/`)

## Step 3 — Opt-in wiring (only when you're ready, one feature at a time)

Each snippet below is something **you add** to `main.py` — I'm not touching
it for you, so you stay in full control and can test each piece in isolation.

### 3a. Hybrid Search + Reranking
```python
from advanced.hybrid_search import HybridSearchEngine

# Load once at startup, from your workshop manuals / parts inventory:
parts_engine = HybridSearchEngine(documents=[
    {"id": "p1", "text": "TATA-9942-OH: oil filter housing assembly for TATA LPT 3118 engines...", "part_number": "TATA-9942-OH"},
    # ... your real inventory/manual chunks here
])

# Inside _run_agentic_rag_diagnostics(), replace your current lookup with:
results = parts_engine.search(incident.issue_type, top_k=3)
```

### 3b. Multi-Agent LangGraph
```python
from advanced.multi_agent_graph import build_fleet_graph

fleet_graph = build_fleet_graph(
    hybrid_search_engine=parts_engine,
    maps_lookup_fn=lambda loc, dest: orchestrator._get_heavy_service_center_intelligence()
)
# Run per-incident with config={"configurable": {"thread_id": incident_id}}
```

### 3c. Self-RAG / CRAG / Hallucination Guard
```python
from advanced.self_rag import self_rag_pipeline

output = self_rag_pipeline(
    query=incident.issue_type,
    retrieved_docs=results,
    generate_answer_fn=lambda q, docs: call_ai_agent(q, {"docs": docs}),
    fallback_search_fn=lambda q: parts_engine.search(q, top_k=5),
)
if not output["safe_to_show_user"]:
    # flagged: irrelevant context or an invented part number -> escalate to human
    ...
```

### 3d. Observability
```python
from advanced.observability import traced, TraceContext

@traced("mechanic_parts_rag_search")
def search_parts(query):
    return parts_engine.search(query)

# Or wrap a whole incident run:
with TraceContext("incident_run", vehicle_id=incident.vehicle_id) as ctx:
    result = orchestrator.run_swarm()
```
Enable a real backend by setting env vars (`LANGCHAIN_TRACING_V2=true` +
`LANGCHAIN_API_KEY` for LangSmith, or `OTEL_EXPORTER_OTLP_ENDPOINT` for
OpenTelemetry / Arize Phoenix). Without either set, it just prints
structured JSON trace lines instead of your current `print("DEBUG: ...")`.

### 3e. Multi-Modal (WhatsApp image -> part identification)
```python
from advanced.multimodal import process_whatsapp_image_message

# Inside your existing whatsapp_webhook handler, when message type == "image":
media_id = message["image"]["id"]
vision_result = process_whatsapp_image_message(media_id, vehicle_id=vehicle_id)
if vision_result.get("suggested_search_query"):
    results = parts_engine.search(vision_result["suggested_search_query"], top_k=3)
```

## Notes / things to set up on your side

- **API keys needed for full functionality**: `COHERE_API_KEY` (or set
  `USE_LOCAL_RERANKER=1` to use a free local BGE model instead),
  `LANGCHAIN_API_KEY` (LangSmith, optional), `GEMINI_API_KEY` (you already
  have this for vision + grading calls).
- All 5 modules are written to **degrade gracefully** — if a key or package
  is missing, they log a warning and fall back to a safe default instead of
  crashing your app.
- `multi_agent_graph.py` uses LangGraph's `MemorySaver` checkpointer for the
  demo — swap to `SqliteSaver` or `PostgresSaver` before production so the
  Human-in-the-Loop pause survives a server restart.
- Real workshop manual / inventory text needs to be loaded into
  `HybridSearchEngine` — currently the integration example uses placeholder
  documents; replace with your real TATA/engine parts data.

Koi bhi step chalane se pehle ek baar `git diff` check kar lena — is guide
mein diya gaya har code snippet sirf reference hai, khud paste karke apne
hisaab se adjust karna.
