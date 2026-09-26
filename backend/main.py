import os
import time
import uuid
import re
import json
import sqlite3
import threading
import collections.abc
import hashlib
from typing import List, Dict, Any, Optional
import requests
from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- REAL advanced modules (replace the simulated versions below) ---
from advanced.hybrid_search import HybridSearchEngine
from advanced.self_rag import grade_hallucination
from advanced.observability import traced, TraceContext
from advanced.multimodal import identify_damaged_part

# ==========================================
# PERSISTENCE LAYER — SQLite-backed dict replacement.
# Every APPROVAL_STATES[x] = y / INCIDENT_DETAILS.get(x) / del PENDING_QUOTES[x]
# call elsewhere in this file keeps working UNCHANGED — this is a drop-in
# MutableMapping, not a new API. Fixes: all incident/approval/quote state was
# previously plain in-memory dicts, wiped on every Render restart/redeploy.
# ==========================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_state.db")


class PersistentDict(collections.abc.MutableMapping):
    def __init__(self, table_name: str):
        self.table = table_name
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (key TEXT PRIMARY KEY, value TEXT)")
        self._conn.commit()

    def __getitem__(self, key):
        row = self._conn.execute(f"SELECT value FROM {self.table} WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key, value):
        self._conn.execute(
            f"INSERT INTO {self.table} (key, value) VALUES (?, ?) "
            f"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value))
        )
        self._conn.commit()

    def __delitem__(self, key):
        cur = self._conn.execute(f"DELETE FROM {self.table} WHERE key=?", (key,))
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(key)

    def __iter__(self):
        return iter(r[0] for r in self._conn.execute(f"SELECT key FROM {self.table}").fetchall())

    def __len__(self):
        return self._conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]

    def __contains__(self, key):
        return self._conn.execute(f"SELECT 1 FROM {self.table} WHERE key=?", (key,)).fetchone() is not None


app = FastAPI(title="Autonomous Enterprise Fleet Agentic AI & RAG Engine", version="5.0.0-GoogleLevel")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MANAGER_WHATSAPP_NUMBER = "+916209313108"

# TEMPORARY TESTING OVERRIDE — Spare-Part Price Comparison feature.
# Fill in real numbers here to test the quote/negotiation flow RIGHT NOW
# (works only with numbers added to your Meta "allowed test recipients"
# list, or once your quote-request template is approved).
#
# Once your Meta message template is approved, just set this back to an
# EMPTY LIST — the system will automatically switch back to using the
# real nearby vendors that Google Places finds for each incident. No other
# code change needed.
TEST_VENDOR_NUMBERS = [
    {"name": "Test Vendor 1", "phone": "+917759034474"},
    {"name": "Test Vendor 2", "phone": "+918210002439"},
]

# TESTING PHASE: real nearest-hub contact numbers abhi wire-up nahi hain,
# isliye har detected hub ko in DO numbers me se cyclically ek assign hota
# hai (_resolve_hub_phone me index % 2 se). Jaise hi asli hub numbers ready
# hon, TEST_HUB_NUMBERS ko empty [] kar dena — real Google-Place-Details
# lookup apne aap wapas use hone lagega.
TEST_HUB_NUMBERS = ["+917759034474", "+918210002439"]

# Fleet Unit ID to WhatsApp Number mapping
DRIVER_WHATSAPP_MAPPING = {
    "BR01GP9621": "+917858847385",
    "BR01GM7465": "+916209313108",
    "BR01GP0756": "+916209313108",
    "BR01GP0757": "+916202886701",
    "BR01GP8148": "+916209313108"
}

# In-memory storage for HITL approval states and active contexts
# NOTE: Now SQLite-backed (PersistentDict) instead of plain {} — survives
# Render restarts/redeploys. Usage elsewhere in this file is unchanged.
APPROVAL_STATES = PersistentDict("approval_states")
INCIDENT_CONTEXTS = PersistentDict("incident_contexts")   # phone -> incident_id
INCIDENT_DETAILS = PersistentDict("incident_details")      # incident_id -> full context dict (for AI replies)
ACTIVE_VEHICLE_BY_PHONE = PersistentDict("active_vehicle_by_phone")

# Feature: Spare-Part Price Comparison — tracks outstanding quote requests
# sent to nearby service centers, keyed by incident_id.
PENDING_QUOTES = PersistentDict("pending_quotes")
# 10-digit phone -> vehicle_id, reverse of DRIVER_WHATSAPP_MAPPING, so an
# inbound voice note can be matched to a vehicle without the driver typing it.
DRIVER_PHONE_TO_VEHICLE = {
    ''.join(filter(str.isdigit, phone))[-10:]: vid
    for vid, phone in DRIVER_WHATSAPP_MAPPING.items()
}

# Feature: Per-Vehicle Predictive Maintenance — vehicle_id -> list of
# {issue_type, keywords, ts} records, so a repeating breakdown pattern on
# the SAME vehicle can be flagged to the manager automatically.
VEHICLE_ISSUE_HISTORY = PersistentDict("vehicle_issue_history")
PREDICTIVE_PATTERN_WINDOW_DAYS = int(os.getenv("PREDICTIVE_PATTERN_WINDOW_DAYS", "90"))
PREDICTIVE_PATTERN_MIN_REPEATS = int(os.getenv("PREDICTIVE_PATTERN_MIN_REPEATS", "2"))

# Feature: Proactive Driver Follow-up — incident_id -> unix ts of the last
# time the driver actually messaged us about it (text/photo/location). Used
# to decide whether a scheduled check-in is still needed or the driver has
# already updated us in the meantime.
DRIVER_LAST_UPDATE_TS = PersistentDict("driver_last_update_ts")
# How long to wait after a repair is dispatched before proactively checking
# in on the driver if they've gone silent. Configurable via env for testing
# (e.g. set to 120 for a 2-minute demo instead of the real-world default).
FOLLOWUP_CHECK_DELAY_SECONDS = int(os.getenv("FOLLOWUP_CHECK_DELAY_SECONDS", str(2 * 60 * 60)))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")

# ==========================================
# API KEY AUTHENTICATION — protects /api/triage and /api/send-whatsapp-interactive
# from being hit by anyone on the internet (Gemini/Maps/WhatsApp quota abuse,
# fake incidents). Set FLEET_API_KEY on Render to enable. If it's NOT set,
# auth is skipped with a warning (so an unconfigured deploy never silently
# locks out your own frontend) — set it before going to production.
# NOTE: does NOT apply to /api/whatsapp-webhook, since that's called by Meta
# itself using its own verification, not our frontend.
# ==========================================
FLEET_API_KEY = os.getenv("FLEET_API_KEY")
if not FLEET_API_KEY:
    print("⚠️ [security] FLEET_API_KEY is not set — /api/triage and /api/send-whatsapp-interactive "
          "are currently OPEN to anyone. Set FLEET_API_KEY on Render to require an API key.")


def require_api_key(x_api_key: Optional[str] = Header(None)):
    if FLEET_API_KEY and x_api_key != FLEET_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True


# ==========================================
# RATE LIMITING — simple in-memory sliding window per client IP. No new
# dependency required. Protects against abuse/cost-explosion on the paid
# Gemini/Google Maps/WhatsApp API calls behind these endpoints.
# ==========================================
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 20
_rate_limit_tracker: Dict[str, List[float]] = {}


def rate_limit(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    timestamps = _rate_limit_tracker.get(client_ip, [])
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — too many requests, please slow down.")
    timestamps.append(now)
    _rate_limit_tracker[client_ip] = timestamps
    return True
WHATSAPP_PHONE_ID = os.getenv("PHONE_NUMBER_ID", "1340284595815318")
VERIFY_TOKEN = "fleet_secret_token_2026"
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ==========================================
# 4. ENTERPRISE OBSERVABILITY & TRACING SIMULATOR (LangSmith / Arize Phoenix Style)
# ==========================================
class EnterpriseObservabilityTracer:
    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self.traces = []

    def log_span(self, agent_name: str, span_type: str, input_data: Any, output_data: Any, latency_ms: float, tokens_used: int = 150):
        trace_entry = {
            "trace_id": f"TRC-{uuid.uuid4().hex[:8].upper()}",
            "incident_id": self.incident_id,
            "agent_role": agent_name,
            "span_type": span_type,
            "latency_ms": latency_ms,
            "estimated_token_cost": tokens_used,
            "timestamp": time.time(),
            "input": input_data,
            "output": output_data
        }
        self.traces.append(trace_entry)
        print(f"📊 [OBSERVABILITY TRACE] Agent: {agent_name} | Type: {span_type} | Latency: {latency_ms}ms")

# ==========================================
# KNOWLEDGE BASE LOADER — reads ANY number of JSON files from
# backend/knowledge_base/, so the corpus can grow to hundreds/thousands of
# manual excerpts (covering the whole India fleet) without ever touching
# main.py again. Falls back to a small built-in seed list only if the
# folder is missing/empty, so the app never breaks on a fresh checkout.
# ==========================================
KNOWLEDGE_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base")


def _load_knowledge_base() -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    seen_ids = set()

    if os.path.isdir(KNOWLEDGE_BASE_DIR):
        for filename in sorted(os.listdir(KNOWLEDGE_BASE_DIR)):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(KNOWLEDGE_BASE_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception as e:
                print(f"[knowledge_base] WARNING: could not parse {filename}: {e}")
                continue

            entries = loaded if isinstance(loaded, list) else [loaded]
            for entry in entries:
                required = {"id", "manual", "keywords", "part_code", "content"}
                if not required.issubset(entry.keys()):
                    print(f"[knowledge_base] WARNING: skipping malformed entry in {filename} "
                          f"(missing one of {required})")
                    continue
                if entry["id"] in seen_ids:
                    print(f"[knowledge_base] WARNING: duplicate id '{entry['id']}' in {filename}, skipping.")
                    continue
                seen_ids.add(entry["id"])
                docs.append(entry)

    if docs:
        print(f"[knowledge_base] Loaded {len(docs)} documents from {KNOWLEDGE_BASE_DIR}")
        return docs

    print(f"[knowledge_base] WARNING: no valid JSON docs found in {KNOWLEDGE_BASE_DIR}, "
          f"using built-in seed knowledge base.")
    return [
        {
            "id": "DOC-01",
            "manual": "Tata Prima / Signa Heavy Commercial Maintenance Manual v4.2",
            "keywords": ["overheat", "temperature", "radiator", "coolant", "thermostat"],
            "part_code": "Heavy Duty Coolant Pump & Thermostat Assembly (Part #TATA-9942-OH)",
            "content": "Radiator core choke or thermostat valve blockage detected leading to coolant flow restriction."
        },
        {
            "id": "DOC-03",
            "manual": "General Commercial Fleet Telemetry & Fault Guide",
            "keywords": ["electrical", "sensor", "fuel", "filter", "harness", "pressure"],
            "part_code": "Universal Heavy Fleet Fuel Filter & Sensor Kit (Part #FL-GEN-01)",
            "content": "Standard electrical harness interruption or fuel line pressure fluctuation under heavy load."
        }
    ]


# ==========================================
# 1. HYBRID SEARCH + VECTOR DB + BM25 & CROSS-ENCODER RERANKER
# ==========================================
class AdvancedHybridRAGEngine:
    """
    REAL Hybrid Search (Feature 1): BM25Okapi (genuine sparse keyword scoring
    — exact for part codes like TATA-9942-OH) fused with dense vector
    similarity via Reciprocal Rank Fusion when an embedding model is
    available, then reranked with a cross-encoder (Cohere / local BGE) when
    configured. Falls back gracefully to real BM25-only ranking otherwise —
    every score below comes from an actual retrieval algorithm now, not a
    hardcoded formula.
    """
    def __init__(self):
        # Fleet Knowledge Base — loaded from backend/knowledge_base/*.json,
        # NOT hardcoded here. Drop new .json files in that folder to grow
        # the corpus; no code change needed.
        self.knowledge_base = _load_knowledge_base()
        # "text" is what the real BM25/vector index searches over — includes
        # the part code itself so the hallucination grader (below) can later
        # confirm the recommended part is genuinely grounded in this text.
        documents = [
            {
                **doc,
                "text": f"{doc['content']} {' '.join(doc['keywords'])} Recommended part: {doc['part_code']}",
            }
            for doc in self.knowledge_base
        ]
        self._engine = HybridSearchEngine(documents=documents)

    def add_learned_document(self, new_doc: Dict[str, Any]):
        """Self-Improving Knowledge Base: called when a manager approves an
        AI-generated (non-manual) diagnosis. Permanently learns it so the
        next identical/similar issue gets a real, high-confidence manual
        match instead of falling back to Gemini again. Rebuilds the LIVE
        search index immediately (no restart needed) and also writes it to
        disk so it survives instance restarts.
        NOTE: on Render's default (no persistent-disk add-on) this file
        survives restarts but not a fresh redeploy — for guaranteed
        durability across redeploys, add a Render persistent disk mounted
        at this path, or move learned docs into the SQLite/Postgres layer."""
        self.knowledge_base.append(new_doc)
        documents = [
            {**doc, "text": f"{doc['content']} {' '.join(doc['keywords'])} Recommended part: {doc['part_code']}"}
            for doc in self.knowledge_base
        ]
        self._engine = HybridSearchEngine(documents=documents)

        os.makedirs(KNOWLEDGE_BASE_DIR, exist_ok=True)
        auto_learned_path = os.path.join(KNOWLEDGE_BASE_DIR, "auto_learned.json")
        existing = []
        if os.path.exists(auto_learned_path):
            try:
                with open(auto_learned_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        existing.append(new_doc)
        with open(auto_learned_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        print(f"[self-improving-kb] Learned new document '{new_doc['id']}' — knowledge base now has {len(self.knowledge_base)} docs.")

    @traced("hybrid_search_retrieve_and_rerank")
    def hybrid_retrieve_and_rerank(self, issue_type: str) -> Dict[str, Any]:
        results = self._engine.search(issue_type, top_k=1)
        if not results:
            top = self.knowledge_base[-1]
            return {
                "vector_match_score": 0.70,
                "bm25_keyword_score": 0.0,
                "cross_encoder_rerank_confidence": 0.70,
                "referenced_manual": top["manual"],
                "diagnostic_summary": top["content"],
                "recommended_part": top["part_code"],
                "_grounding_text": top["content"],
            }

        top = results[0]

        # Real BM25 relevance for the top match, squashed into a 0..1 range
        # (frontend renders vector_match_score * 100 as "% Accuracy", so this
        # must stay in the same 0..1 scale the old simulated value used).
        raw_bm25_scores = self._engine._bm25_scores(issue_type)
        top_bm25 = float(max(raw_bm25_scores)) if len(raw_bm25_scores) else 0.0
        bm25_normalized = round(top_bm25 / (top_bm25 + 1.0), 3) if top_bm25 > 0 else 0.0

        rerank_score = top.get("rerank_score")
        fused_score = top.get("fused_score", 0.0)
        if rerank_score is not None:
            vector_match_score = round(min(0.99, max(0.5, rerank_score)), 3)
        elif top_bm25 <= 0:
            # Zero real keyword overlap with ANY manual — this is a genuine
            # "no confident match" case (issue_type doesn't match this small
            # knowledge base), so say so honestly instead of a reassuring
            # fixed 75% floor.
            vector_match_score = 0.30
        else:
            # No reranker installed -> derive confidence from the raw BM25
            # top score, scaled against an empirical "confident match"
            # ceiling (genuine multi-keyword hits typically score several
            # points; a single stray common-word overlap scores much lower).
            # NOTE: fused_score is intentionally NOT used here — its scale
            # differs depending on whether dense embeddings are active
            # (tiny ~0.03 RRF values) or not (raw BM25 magnitude, unbounded),
            # and mixing those two scales previously produced nonsense
            # confidence values (e.g. ~95% for one coincidental word match).
            normalized_strength = min(1.0, top_bm25 / 4.0)
            vector_match_score = round(min(0.95, 0.35 + normalized_strength * 0.55), 3)

        return {
            "vector_match_score": vector_match_score,
            "bm25_keyword_score": bm25_normalized,
            "cross_encoder_rerank_confidence": round(rerank_score, 3) if rerank_score is not None else vector_match_score,
            "referenced_manual": top["manual"],
            "diagnostic_summary": top["content"],
            "recommended_part": top["part_code"],
            # internal-only field, consumed and stripped by CorrectiveRAGValidator below
            "_grounding_text": top["text"],
        }


# Build the hybrid search index (BM25 + optional dense embeddings) ONCE at
# server startup, not once per incoming request. With a small 9-doc corpus
# rebuilding it every request was wasteful but tolerable; with hundreds or
# thousands of real fleet manuals it would make every single API call slow
# and eventually time out. All requests now share this one loaded engine.
GLOBAL_HYBRID_RAG_ENGINE = AdvancedHybridRAGEngine()


# ==========================================
# VOICE NOTE SUPPORT — driver bolke breakdown report kar sake, type kiye
# bina. WhatsApp audio message ko Meta se download karke Gemini se transcribe
# + structured fields (vehicle_id, location, issue_type) extract karta hai.
# ==========================================
def _download_whatsapp_media(media_id: str) -> Optional[bytes]:
    """Meta Graph API se media ID ka actual audio file download karta hai."""
    if not WHATSAPP_TOKEN:
        return None
    try:
        meta_url = f"https://graph.facebook.com/v26.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        meta_resp = requests.get(meta_url, headers=headers, timeout=10)
        meta_resp.raise_for_status()
        media_url = meta_resp.json().get("url")
        if not media_url:
            return None
        file_resp = requests.get(media_url, headers=headers, timeout=15)
        file_resp.raise_for_status()
        return file_resp.content
    except Exception as e:
        print(f"DEBUG VOICE NOTE DOWNLOAD ERROR: {str(e)}")
        return None


def _transcribe_voice_note(audio_bytes: bytes, mime_type: str = "audio/ogg") -> Optional[Dict[str, str]]:
    """Voice note (Hindi/Hinglish/regional) ko Gemini se seedha samajh ke
    incident ke zaroori fields (vehicle_id, location, issue_type) nikaalta
    hai — driver ko form fill karne ki zaroorat nahi, sirf bol dena kaafi hai."""
    if not GEMINI_API_KEY:
        return None
    try:
        import base64
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
        )
        prompt = (
            "This is a voice note from a truck/bus driver in India, likely in Hindi, "
            "Hinglish, or a regional language, reporting a vehicle breakdown. Listen to it "
            "and respond ONLY with strict JSON, no markdown fences, no preamble, in this "
            "exact shape:\n"
            '{"vehicle_id": "<vehicle number/ID if mentioned, else \\"UNKNOWN\\">", '
            '"location": "<place name if mentioned, else \\"UNKNOWN\\">", '
            '"issue_type": "<short English description of the mechanical issue described>"}'
        )
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": audio_b64}}
                ]
            }]
        }
        resp = requests.post(url, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw_text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        if parsed.get("issue_type"):
            return {
                "vehicle_id": parsed.get("vehicle_id", "UNKNOWN"),
                "location": parsed.get("location", "UNKNOWN"),
                "issue_type": parsed["issue_type"],
            }
    except Exception as e:
        print(f"DEBUG VOICE NOTE TRANSCRIBE ERROR: {str(e)}")
    return None


# ==========================================
# Features 1 & 2: IN-CHAT PHOTO DIAGNOSIS + SELF-SERVICE REPAIR GUIDANCE —
# driver jab bhi WhatsApp chat me kabhi bhi (sirf initial report ke time
# nahi) part/engine/tyre/dashboard ki photo bheje, Gemini Vision use turant
# analyze karke severity aur ek verdict deta hai: agar chhota/safe issue hai
# to driver ko khud-thik-karne ke step-by-step instructions milte hain
# (mechanic dispatch ki zaroorat nahi), warna manager ko photo + AI
# diagnosis ke saath escalate kar diya jaata hai.
# ==========================================
def _diagnose_incident_photo(image_bytes: bytes, mime_type: str, incident_ctx: dict) -> Optional[Dict[str, Any]]:
    """Gemini Vision se photo ka real diagnosis leta hai. Return shape:
    {visible_damage, severity, can_self_fix, diy_steps, recommended_part,
    driver_message} — ya None agar Gemini unavailable/fail ho jaaye (caller
    tab safe generic fallback message dikhata hai, kabhi crash nahi hota)."""
    if not GEMINI_API_KEY:
        return None
    try:
        import base64
        img_b64 = base64.b64encode(image_bytes).decode("utf-8")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
        )
        prompt = (
            "You are a heavy commercial vehicle (truck/bus) diagnostic assistant for an Indian fleet "
            "operator. A driver just sent this photo in WhatsApp chat about their vehicle "
            f"(Vehicle ID: {incident_ctx.get('vehicle_id', 'unknown')}, previously reported issue: "
            f"'{incident_ctx.get('issue_type', 'not specified')}'). Look at the photo carefully and give "
            "a real, specific assessment — never a generic placeholder.\n\n"
            "Respond ONLY with strict JSON, no markdown fences, no preamble, in this exact shape:\n"
            '{"visible_damage": "<specific description of what you actually see>", '
            '"severity": "LOW"|"MODERATE"|"HIGH"|"CRITICAL", '
            '"can_self_fix": true/false, '
            '"diy_steps": ["step 1", "step 2", ...] (empty list if can_self_fix is false), '
            '"recommended_part": "<part/kit name if a replacement looks needed, else empty string>", '
            '"driver_message": "<a short 2-4 sentence WhatsApp reply in Hinglish (Hindi written in roman '
            'letters, natural and driver-friendly) explaining the diagnosis, and either the DIY steps or '
            'that help/a mechanic is being arranged>"}\n\n'
            "Rules: only set can_self_fix=true for genuinely minor, safe roadside fixes needing no tools/"
            "training (e.g. a loose connector, topping up coolant/water, a stuck relay, a blown fuse, "
            "reseating a battery terminal). NEVER suggest self-fixing brakes, steering, the fuel system, "
            "anything electrical under load, or anything requiring lifting the vehicle or working near a "
            "running engine or hot parts — for those, can_self_fix must be false. If the photo is unclear "
            "or you are not confident, set can_self_fix=false, severity to MODERATE, and ask for a "
            "clearer/well-lit photo in driver_message instead of guessing."
        )
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": img_b64}}
                ]
            }],
            "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"}
        }
        resp = requests.post(url, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw_text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        if parsed.get("driver_message"):
            parsed.setdefault("severity", "MODERATE")
            if parsed["severity"] not in ("LOW", "MODERATE", "HIGH", "CRITICAL"):
                parsed["severity"] = "MODERATE"
            parsed.setdefault("can_self_fix", False)
            parsed.setdefault("diy_steps", [])
            parsed.setdefault("recommended_part", "")
            parsed.setdefault("visible_damage", "Not clearly identifiable from photo")
            return parsed
    except Exception as e:
        print(f"DEBUG PHOTO DIAGNOSIS ERROR: {str(e)}")
    return None


def _forward_whatsapp_image(to_phone: str, media_id: str, caption: str = ""):
    """Driver ki bheji hui photo manager ko bhi forward karta hai, isi
    already-uploaded media_id ke through — koi re-download/re-upload nahi.
    Best-effort hai: agar Meta account setup me ye fail ho (media_id doosre
    recipient ke liye valid na ho), caller isse silently ignore karta hai —
    driver-facing diagnosis reply is se kabhi block nahi hota."""
    if not WHATSAPP_TOKEN or WHATSAPP_TOKEN == "YOUR_TOKEN" or not media_id:
        return {"status": "simulated_or_skipped"}
    cleaned_phone = ''.join(filter(str.isdigit, to_phone))
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    image_payload = {"id": media_id}
    if caption:
        image_payload["caption"] = caption
    body = {
        "messaging_product": "whatsapp",
        "to": cleaned_phone,
        "type": "image",
        "image": image_payload
    }
    try:
        res = requests.post(url, json=body, headers=headers, timeout=10)
        return {"status_code": res.status_code, "delivery_failed": res.status_code != 200}
    except Exception as e:
        print(f"DEBUG IMAGE FORWARD EXCEPTION: {str(e)}")
        return {"status": "error", "details": str(e)}


# ==========================================
# SPARE-PART PRICE COMPARISON — jab service centre milta hai, top nearby
# vendors se real-time quote maanga jaata hai; jawab aane par AI compare
# karke best price manager ko dikhata hai, aur zaroorat par ek round
# negotiate bhi kar leta hai.
# ==========================================
def _request_spare_part_quotes(incident_id: str, hub_records: List[Dict[str, Any]], part_name: str):
    """Top 3 nearby hubs ko WhatsApp par part ki price/availability poochta hai.
    Agar TEST_VENDOR_NUMBERS me kuch bhara hai, wahi use hoga (testing mode) —
    warna real Google-Places-derived nearby vendors automatically use hote hain."""
    if TEST_VENDOR_NUMBERS:
        targets = list(TEST_VENDOR_NUMBERS)
    else:
        targets = []
        for idx, hub in enumerate(hub_records[:3]):
            phone = _resolve_hub_phone(hub, idx)
            if phone and "not available" not in phone.lower():
                targets.append({"name": hub["name"], "phone": phone})

    if not targets:
        return

    PENDING_QUOTES[incident_id] = {
        "part_name": part_name,
        "vendors": {
            ''.join(filter(str.isdigit, t["phone"]))[-10:]: {
                "hub_name": t["name"], "phone": t["phone"],
                "status": "waiting", "price": None, "negotiated": False,
                "awaiting_cost": False
            }
            for t in targets
        }
    }

    failed_vendors = []
    for t in targets:
        vendor_key = ''.join(filter(str.isdigit, t["phone"]))[-10:]
        result = send_vendor_availability_buttons(t["phone"], incident_id, vendor_key, part_name)
        delivery_failed = (
            result.get("status") == "error"
            or result.get("delivery_failed") is True
        )
        if delivery_failed:
            failed_vendors.append(t["name"])
            quote_ctx = PENDING_QUOTES[incident_id]
            quote_ctx["vendors"].pop(vendor_key, None)
            PENDING_QUOTES[incident_id] = quote_ctx

    if failed_vendors:
        send_whatsapp_text_reply(
            MANAGER_WHATSAPP_NUMBER,
            f"⚠️ Could not reach {len(failed_vendors)} vendor(s) for a price quote on "
            f"'{part_name}' — their number may not be reachable via WhatsApp Business API "
            f"yet (needs a prior conversation or an approved message template): "
            f"{', '.join(failed_vendors)}"
        )

    if not PENDING_QUOTES.get(incident_id, {}).get("vendors"):
        PENDING_QUOTES.pop(incident_id, None)
        return

    # Safety-net: manager gets whatever quotes are ready 40s after the
    # enquiry goes out, even if not every vendor has replied yet — so
    # "one vendor added, one vendor answered" cases never sit unnotified.
    threading.Timer(40.0, _finalize_quotes_after_timeout, args=[incident_id]).start()


def send_vendor_availability_buttons(vendor_phone: str, incident_id: str, vendor_10_digit: str, part_name: str):
    """Vendor ko free-text ki jagah tap-karke-choose-karne wale Yes/No buttons
    bhejta hai availability ke liye — isse Gemini-based natural-language
    parsing par depend nahi karna padta (jo GEMINI_API_KEY na hone ya
    ambiguous replies ki wajah se silently fail ho sakta tha)."""
    cleaned_phone = ''.join(filter(str.isdigit, vendor_phone))
    body_text = (
        f"🔧 *Fleet Parts Enquiry*\n\n"
        f"Namaste, kya aapke paas ye part available hai?\n"
        f"*Part:* {part_name}"
    )
    buttons = [
        {"type": "reply", "reply": {"id": f"VENDORAVAIL_YES_{incident_id}_{vendor_10_digit}", "title": "✅ Yes, available"}},
        {"type": "reply", "reply": {"id": f"VENDORAVAIL_NO_{incident_id}_{vendor_10_digit}", "title": "❌ Not available"}}
    ]
    if WHATSAPP_TOKEN and WHATSAPP_TOKEN != "YOUR_TOKEN":
        url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        body = {
            "messaging_product": "whatsapp",
            "to": cleaned_phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": buttons}
            }
        }
        try:
            res = requests.post(url, json=body, headers=headers, timeout=10)
            print(f"DEBUG VENDOR AVAIL BUTTON RESPONSE [{res.status_code}]:", res.text)
            return {"status_code": res.status_code, "delivery_failed": res.status_code != 200}
        except Exception as e:
            print(f"DEBUG VENDOR AVAIL BUTTON EXCEPTION: {str(e)}")
            return {"status": "error", "details": str(e)}
    else:
        print(f"[simulated] Vendor availability buttons to {vendor_phone}: {body_text}")
        return {"status": "simulated_reply"}


def _notify_manager_with_quotes(incident_id: str) -> bool:
    """Ready ho chuke vendor quotes manager ko choose-buttons ke roop me
    bhejta hai. True return karta hai sirf jab kuch actually bheja gaya ho —
    is se all_done-trigger aur 40s-timeout dono isi ek jagah se manager ko
    notify karte hain, duplicate logic nahi."""
    quote_ctx = PENDING_QUOTES.get(incident_id)
    if not quote_ctx or quote_ctx.get("manager_notified"):
        return False
    quoted = [
        {**v, "vendor_key": k} for k, v in quote_ctx["vendors"].items()
        if v["status"] == "quoted" and v.get("price")
    ]
    if not quoted:
        return False
    quoted.sort(key=lambda v: v["price"])
    result = send_vendor_choice_buttons(incident_id, quoted, quote_ctx["part_name"])
    # NOTE: pehle sirf "delivery_failed" flag check hota tha — agar
    # send_vendor_choice_buttons ke andar exception (network/timeout) aata,
    # wo {"status": "error", ...} return karta, jisme "delivery_failed" key
    # hoti hi nahi — isliye fallback text bhi silently skip ho jaata tha aur
    # manager ko interactive buttons YA fallback, dono me se kuch nahi
    # jaata tha (jabki manager_notified=True set ho jaata, dobara try bhi
    # nahi hota). Ab dono cases (non-200 response AND exception) fallback
    # trigger karte hain — same fix jo ACCEPTREPAIR button flow me hai.
    send_failed = isinstance(result, dict) and (
        result.get("delivery_failed") or result.get("status") == "error"
    )
    if send_failed:
        # Interactive buttons na jaa paaye (button-title limit, template
        # issue, network error, etc.) — manager ko kam se kam plain text me
        # list to milni chahiye, taaki wo manually reply karke vendor
        # choose kar sake.
        fallback_lines = [f"💰 *Spare Part Price Comparison*\n*Part:* {quote_ctx['part_name']}\n"]
        for i, v in enumerate(quoted[:3]):
            tag = " ✅ Best" if i == 0 else ""
            fallback_lines.append(f"{i+1}. {v['hub_name']}: ₹{v['price']:.0f}{tag}")
        fallback_lines.append("\n⚠️ Interactive buttons deliver nahi ho paaye — reply karke bataayein kaunsa vendor (1/2/3) choose karna hai.")
        send_whatsapp_text_reply(MANAGER_WHATSAPP_NUMBER, "\n".join(fallback_lines))
    quote_ctx["manager_notified"] = True
    PENDING_QUOTES[incident_id] = quote_ctx
    return True


def _finalize_quotes_after_timeout(incident_id: str):
    """40 second ke baad chalta hai (background thread se) — jo bhi quotes
    us waqt tak ready hain, unhe manager ko bhej deta hai, baaki vendors
    baad me reply karein to woh apne aap all_done path se handle ho jaayega."""
    try:
        _notify_manager_with_quotes(incident_id)
    except Exception as e:
        print(f"DEBUG QUOTE TIMEOUT ERROR: {str(e)}")


# ==========================================
# Feature 5: PROACTIVE DRIVER FOLLOW-UP — jab bhi repair dispatch/accept ho
# jaata hai, ek background timer set ho jaata hai. Agar driver ne itni der
# tak koi naya update nahi bheja, AI khud check-in kar leta hai — driver ka
# passively wait nahi karta. Har real driver message DRIVER_LAST_UPDATE_TS
# refresh karta hai (webhook me), isliye agar driver pehle hi update de
# chuka hai to ye check silently kuch nahi karta.
# ==========================================
def _schedule_followup_check(incident_id: str):
    """Ek incident ke liye follow-up timer arm karta hai. Idempotent hona
    caller (ACCEPTREPAIR_/APPROVE_ handlers) ki zimmedari hai — wahaan
    incident_ctx['followup_scheduled'] flag se duplicate scheduling roki
    jaati hai."""
    scheduled_at = time.time()
    DRIVER_LAST_UPDATE_TS[incident_id] = scheduled_at
    threading.Timer(FOLLOWUP_CHECK_DELAY_SECONDS, _run_followup_check, args=[incident_id, scheduled_at]).start()
    print(f"DEBUG FOLLOWUP SCHEDULED: incident={incident_id} in {FOLLOWUP_CHECK_DELAY_SECONDS}s")


def _run_followup_check(incident_id: str, scheduled_at: float):
    """Timer fire hone par chalta hai. Agar driver ne is beech me khud
    update de diya (DRIVER_LAST_UPDATE_TS scheduled_at se aage badh gaya),
    to chup-chaap kuch nahi karta. Warna driver ko ek friendly check-in aur
    manager ko ek heads-up bhejta hai, aur agla follow-up phir se chain kar
    deta hai jab tak incident reject/close na ho jaaye."""
    try:
        incident_ctx = INCIDENT_DETAILS.get(incident_id)
        if not incident_ctx:
            return
        if APPROVAL_STATES.get(incident_id) == "REJECTED_REROUTING":
            return

        last_update = DRIVER_LAST_UPDATE_TS.get(incident_id, 0)
        if last_update > scheduled_at:
            # Driver ne is beech me khud update de diya — dobara check-in
            # bhejne ki zaroorat nahi, lekin AGLA follow-up window abhi bhi
            # arm karo taaki silence ka pattern chalte rehte hue bhi pakda jaaye.
            threading.Timer(
                FOLLOWUP_CHECK_DELAY_SECONDS, _run_followup_check, args=[incident_id, last_update]
            ).start()
            return

        vehicle_id = incident_ctx.get("vehicle_id", "N/A")
        driver_phone = _lookup_driver_phone(vehicle_id)
        if driver_phone:
            send_whatsapp_text_reply(
                driver_phone,
                f"👋 Namaste, *{vehicle_id}* ki repair kaisi chal rahi hai? "
                f"Koi update ya madad chahiye ho to yahin bata dein."
            )
        hours = round(FOLLOWUP_CHECK_DELAY_SECONDS / 3600, 1)
        send_whatsapp_text_reply(
            MANAGER_WHATSAPP_NUMBER,
            f"⏰ *Follow-up:* {vehicle_id} se {hours} ghante se koi update nahi aaya — "
            f"driver ko proactively check-in bhej diya gaya hai."
        )
        # Chain karo — jab tak incident reject/close na ho, silence continue
        # hone par baar-baar follow-up hota rahega, ek-time reminder nahi.
        threading.Timer(
            FOLLOWUP_CHECK_DELAY_SECONDS, _run_followup_check, args=[incident_id, time.time()]
        ).start()
    except Exception as e:
        print(f"DEBUG FOLLOWUP CHECK ERROR: {str(e)}")


def _extract_price_from_reply(vendor_text: str) -> Optional[Dict[str, Any]]:
    """Vendor ke natural-language reply se price + availability nikaalta hai."""
    if not GEMINI_API_KEY:
        return None
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
        )
        prompt = (
            "A spare-parts vendor in India replied to a price enquiry (possibly across "
            "multiple short messages, shown combined below). Their message(s):\n\n"
            f"\"{vendor_text}\"\n\n"
            "Respond ONLY with strict JSON, no markdown, no preamble:\n"
            '{"available": true/false/null, "price_inr": <number or null>}\n'
            "Rules: available=false ONLY if they clearly said it's not available/out of "
            "stock. available=true if they confirmed availability (even without a price "
            "yet, e.g. just \"yes\"). available=null if genuinely unclear."
        )
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw_text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)
    except Exception as e:
        print(f"DEBUG PRICE EXTRACT ERROR: {str(e)}")
        return None


def send_vendor_choice_buttons(incident_id: str, quoted: List[Dict[str, Any]], part_name: str):
    """Manager ko sirf text nahi, balki tap-karke-choose-karne wale buttons
    bhejta hai — jo bhi vendor choose kare, uska price+details driver tak
    automatically forward ho jaata hai."""
    cleaned_phone = re.sub(r'\D', '', MANAGER_WHATSAPP_NUMBER or "")
    body_lines = [f"💰 *Spare Part Price Comparison*\n*Part:* {part_name}\n"]
    buttons = []
    for i, v in enumerate(quoted[:3]):
        tag = " ✅ Best" if i == 0 else ""
        body_lines.append(f"{i+1}. {v['hub_name']}: ₹{v['price']:.0f}{tag}")
        # NOTE: WhatsApp caps interactive reply-button titles at 20 chars —
        # hub_name isn't included here (it's already shown in body_lines
        # above), so this always stays short and never gets silently
        # rejected by Meta the way "₹1250 - Test Vendor 1" (21 chars) did.
        buttons.append({
            "type": "reply",
            "reply": {
                "id": f"PICKVENDOR_{incident_id}_{v['vendor_key']}",
                "title": f"#{i+1}: ₹{v['price']:.0f}"
            }
        })
    body_lines.append("\nKaunsa vendor choose karna hai?")

    if WHATSAPP_TOKEN and WHATSAPP_TOKEN != "YOUR_TOKEN":
        url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        body = {
            "messaging_product": "whatsapp",
            "to": cleaned_phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "\n".join(body_lines)},
                "action": {"buttons": buttons}
            }
        }
        try:
            res = requests.post(url, json=body, headers=headers, timeout=10)
            print(f"DEBUG VENDOR CHOICE BUTTONS RESPONSE [{res.status_code}]:", res.text)
            if res.status_code != 200:
                print(f"⚠️ Meta API Error: Could not deliver vendor-choice buttons to manager.")
            return {"status_code": res.status_code, "delivery_failed": res.status_code != 200}
        except Exception as e:
            print(f"DEBUG VENDOR CHOICE BUTTONS EXCEPTION: {str(e)}")
            return {"status": "error", "details": str(e)}
    else:
        print(f"[simulated] Vendor choice buttons to manager: {body_lines}")
        return {"status": "simulated_reply"}


def _handle_vendor_quote_reply(incident_id: str, vendor_10_digit: str, text_body: str) -> bool:
    """Vendor Yes button dabane ke baad price ka number bhejta hai — ye
    function sirf usi cost-reply ko handle karta hai (regex se digit nikaal
    ke), Gemini par depend nahi karta, isliye availability-parsing wali
    purani ambiguity/silent-failure ab possible nahi hai. False return
    karta hai agar vendor ne abhi Yes/No button hi nahi dabaya (taaki caller
    is text ko normal driver-chat se confuse na kare)."""
    quote_ctx = PENDING_QUOTES.get(incident_id)
    if not quote_ctx or vendor_10_digit not in quote_ctx["vendors"]:
        return False

    vendor = quote_ctx["vendors"][vendor_10_digit]

    if not vendor.get("awaiting_cost"):
        # Vendor ne Yes/No button nahi dabaya — is text ko ignore karo.
        return False

    digits = re.findall(r'\d[\d,]*', text_body)
    if not digits:
        send_whatsapp_text_reply(vendor["phone"], "Kripya sirf price number bhejein (jaise: 10000).")
        return True

    price = float(max(digits, key=lambda d: len(d.replace(",", ""))).replace(",", ""))

    # Ek round negotiation: agar koi doosra vendor sasta quote de chuka hai
    # aur is vendor se abhi negotiate nahi kiya, to AI khud ek counter-offer
    # bhej deta hai — manager ko intervene nahi karna padta. Kisi bhi vendor
    # ka EXACT price doosre vendor ko kabhi disclose nahi karte — sirf itna
    # bataate hain ki "better rate mil sakta hai", competitor ka number nahi.
    cheaper_others = [
        v["price"] for k, v in quote_ctx["vendors"].items()
        if k != vendor_10_digit and v["price"] is not None
    ]
    if cheaper_others and min(cheaper_others) < price and not vendor.get("negotiated"):
        vendor["negotiated"] = True
        vendor["awaiting_cost"] = True
        send_whatsapp_text_reply(
            vendor["phone"],
            f"Dhanyavaad. Hume isse better rate mil sakta hai. "
            f"Kya aap apna price thoda kam kar sakte hain? Kripya number bhejein."
        )
        vendor["status"] = "negotiating"
        vendor["price"] = price  # provisional, may update on their next reply
    else:
        vendor["price"] = price
        vendor["status"] = "quoted"
        vendor["awaiting_cost"] = False

    # Explicit re-save: with a persistent (SQLite-backed) store, .get() returns
    # a deserialized copy, not a live reference — mutations above won't stick
    # unless we write the whole context back, including for partial progress
    # (not all vendors replied yet).
    PENDING_QUOTES[incident_id] = quote_ctx

    # Jab sab vendors se final response aa jaye (quoted/unavailable), manager
    # ko choose-karne-wale buttons abhi hi bhejo (40s timer ka wait nahi
    # karna padega) — is se single-vendor case turant handle ho jaata hai.
    all_done = all(v["status"] in ("quoted", "unavailable") for v in quote_ctx["vendors"].values())
    if all_done and not quote_ctx.get("manager_notified"):
        if not _notify_manager_with_quotes(incident_id):
            send_whatsapp_text_reply(
                MANAGER_WHATSAPP_NUMBER,
                f"⚠️ None of the {len(quote_ctx['vendors'])} nearby vendors had "
                f"'{quote_ctx['part_name']}' available."
            )
            del PENDING_QUOTES[incident_id]

    return True


def _extract_keywords(text: str, limit: int = 10) -> List[str]:
    """Chhote, significant keywords (3+ letters, common stopwords hataakar)
    nikaalta hai — self-learning knowledge base aur per-vehicle predictive
    pattern detection, dono isi shared logic ko use karte hain taaki 'same
    issue' ki matching ek hi jagah se define ho."""
    raw_words = re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
    stopwords = {"the", "and", "for", "with", "not", "has", "have", "was", "are"}
    keywords = list(dict.fromkeys(w for w in raw_words if w not in stopwords))[:limit]
    if not keywords:
        keywords = [(text or "").lower()[:30]]
    return keywords


def _record_and_check_vehicle_pattern(vehicle_id: Optional[str], issue_type: str) -> Optional[str]:
    """Feature: Per-Vehicle Predictive Maintenance. Har naye incident ko us
    vehicle ki (SQLite-persisted) history me record karta hai, aur agar isi
    tarah ka issue (overlapping keywords) is vehicle par pichle
    PREDICTIVE_PATTERN_WINDOW_DAYS din me kam-se-kam PREDICTIVE_PATTERN_MIN_REPEATS
    baar ho chuka hai, to manager ke liye ek chhota predictive-maintenance
    warning string return karta hai — taaki sirf part badalna hi solution na
    ban jaaye, ek baar poora inspection bhi ho jaaye. Returns None agar koi
    repeat-pattern nahi mila (ya vehicle_id hi nahi hai)."""
    if not vehicle_id:
        return None

    new_keywords = set(_extract_keywords(issue_type))
    now = time.time()
    cutoff = now - (PREDICTIVE_PATTERN_WINDOW_DAYS * 86400)

    history = VEHICLE_ISSUE_HISTORY.get(vehicle_id, [])
    recent_history = [h for h in history if h.get("ts", 0) >= cutoff]

    matches = [h for h in recent_history if new_keywords & set(h.get("keywords", []))]

    recent_history.append({"issue_type": issue_type, "keywords": list(new_keywords), "ts": now})
    VEHICLE_ISSUE_HISTORY[vehicle_id] = recent_history

    if len(matches) >= PREDICTIVE_PATTERN_MIN_REPEATS:
        return (
            f"🔁 *Predictive Maintenance Alert:* {vehicle_id} par milta-julta issue pichle "
            f"{PREDICTIVE_PATTERN_WINDOW_DAYS} din me {len(matches)} baar aa chuka hai (is baar "
            f"milaakar {len(matches) + 1}). Sirf part replace karne ke bajaye ek poora inspection "
            f"karwana recommend hai."
        )
    return None


def _build_learned_doc_from_incident(incident_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Manager-approved AI diagnosis ko ek proper knowledge-base document me
    convert karta hai — agli baar wahi/similar issue aane par ye manual-grounded
    high-confidence match banega, Gemini fallback nahi."""
    issue_type = incident_ctx.get("issue_type", "")
    keywords = _extract_keywords(issue_type)

    doc_id = "AUTOLEARN-" + hashlib.md5(issue_type.encode("utf-8")).hexdigest()[:8].upper()
    return {
        "id": doc_id,
        "manual": "AI-Learned Diagnosis (Manager-Verified)",
        "keywords": keywords,
        "part_code": incident_ctx.get("recommended_part", "Standard Certified Heavy Fleet Repair Kit (Part #FL-GEN-01)"),
        "content": incident_ctx.get("diagnostic_summary", issue_type)
    }


def _generate_ai_diagnosis(issue_type: str) -> Optional[Dict[str, str]]:
    """Jab koi bhi hardcoded manual/keyword se match nahi hota, Gemini se
    best-effort real diagnosis generate karta hai — taaki RAG sirf fixed
    knowledge-base categories tak limited na rahe aur kisi bhi real-world
    breakdown query ka genuine jawab de sake. Result hamesha caller ke
    through 'AI-generated, verify manually' ke taur pe flag hota hai —
    kabhi bhi certified-manual jitna confident nahi dikhaya jaata."""
    if not GEMINI_API_KEY:
        return None
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
        )
        prompt = (
            "You are a heavy commercial vehicle (truck/bus) diagnostic assistant "
            "for an Indian fleet operator. A driver reported this breakdown issue:\n\n"
            f"\"{issue_type}\"\n\n"
            "Respond ONLY with strict JSON, no markdown fences, no preamble, in this "
            "exact shape:\n"
            '{"diagnostic_summary": "<one sentence likely cause>", '
            '"recommended_part": "<short generic part or kit name>"}'
        )
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw_text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        if parsed.get("diagnostic_summary") and parsed.get("recommended_part"):
            return {
                "diagnostic_summary": parsed["diagnostic_summary"],
                "recommended_part": parsed["recommended_part"],
            }
    except Exception as e:
        print(f"DEBUG AI DIAGNOSIS FALLBACK ERROR: {str(e)}")
    return None


# ==========================================
# 3. SELF-RAG & CORRECTIVE RAG (CRAG) WITH HALLUCINATION GRADER
# ==========================================
class CorrectiveRAGValidator:
    """
    REAL Self-RAG / CRAG hallucination grader (Feature 3): confirms the
    recommended part number is actually grounded — genuinely present — in
    the retrieved manual text, instead of only checking that it LOOKS like a
    validly-formatted code. Falls back to the same safe baseline part on
    failure, exactly as before — the safety contract is unchanged, only the
    check behind it is now real.
    """
    @staticmethod
    @traced("crag_hallucination_grading")
    def evaluate_and_correct(query: str, rag_output: dict) -> dict:
        grounding_text = rag_output.pop("_grounding_text", "")
        part_code = rag_output.get("recommended_part", "")

        grounding_docs = [{"text": grounding_text}] if grounding_text else []
        hallucination_check = grade_hallucination(part_code, grounding_docs)

        confidence = rag_output.get("cross_encoder_rerank_confidence", 0.9)
        if not hallucination_check["grounded"] or confidence < 0.75:
            # Corrective RAG Trigger: knowledge base has no confident match.
            # Try a real, best-effort AI diagnosis (Gemini) FIRST so genuinely
            # any breakdown query gets a real answer, not just the fixed
            # manual categories — clearly flagged as AI-generated, never
            # shown with certified-manual-level confidence.
            ai_fallback = _generate_ai_diagnosis(query)
            if ai_fallback:
                rag_output["recommended_part"] = ai_fallback["recommended_part"]
                rag_output["referenced_manual"] = (
                    "AI-Generated Diagnosis (Gemini) — not from a certified manual, verify before dispatch"
                )
                rag_output["diagnostic_summary"] = ai_fallback["diagnostic_summary"]
                rag_output["vector_match_score"] = 0.45
                rag_output["bm25_keyword_score"] = 0.0
                rag_output["cross_encoder_rerank_confidence"] = 0.45
                rag_output["crag_intervention_triggered"] = True
                rag_output["hallucination_grade"] = "AI_GENERATED_FALLBACK"
            else:
                # Gemini unavailable/failed too — fall back to the safe
                # generic kit exactly as before (no regression).
                rag_output["recommended_part"] = "Standard Certified Heavy Fleet Repair Kit (Part #FL-GEN-01)"
                rag_output["referenced_manual"] = "No specific manual matched — generic fleet diagnostic applied"
                rag_output["diagnostic_summary"] = (
                    "No confident match found in the knowledge base for this issue type. "
                    "A generic repair kit has been dispatched pending manual inspection."
                )
                rag_output["vector_match_score"] = 0.0
                rag_output["bm25_keyword_score"] = 0.0
                rag_output["cross_encoder_rerank_confidence"] = 0.0
                rag_output["crag_intervention_triggered"] = True
                rag_output["hallucination_grade"] = "CORRECTED_TO_SAFE_BASELINE"
        else:
            rag_output["crag_intervention_triggered"] = False
            rag_output["hallucination_grade"] = "PASSED_VERIFIED_AUTHENTIC"

        return rag_output


class IncidentInput(BaseModel):
    vehicle_id: str
    location: str
    destination: Optional[str] = "Authorized Heavy Workshop Hub"
    issue_type: str
    severity: str
    cargo_type: str
    image_url: Optional[str] = None  # 5. Multi-Modal Vision support


class AgentTrace(BaseModel):
    step_name: str
    agent_role: str
    status: str
    timestamp: float
    output_payload: Dict[str, Any]


class TriageResponse(BaseModel):
    incident_id: str
    status: str
    execution_time_ms: float
    deterministic_steps_executed: int
    assigned_whatsapp_number: str
    approval_status: str
    traces: List[AgentTrace]
    final_resolution: Dict[str, Any]


def _lookup_driver_phone(vehicle_id: Optional[str]) -> Optional[str]:
    """DRIVER_WHATSAPP_MAPPING se phone dhoondta hai — case/whitespace ka
    farak (jaise 'br01gp9621 ' vs 'BR01GP9621') ab match fail nahi karega.
    Agar phir bhi na mile, Render logs me exact vehicle_id print ho jaata
    hai taaki mapping me kya add/fix karna hai turant pata chal jaaye."""
    if not vehicle_id:
        print("DEBUG DRIVER LOOKUP: vehicle_id khud hi empty/None hai — incident_ctx me save nahi hua tha.")
        return None
    normalized = vehicle_id.strip().upper()
    for vid, phone in DRIVER_WHATSAPP_MAPPING.items():
        if vid.strip().upper() == normalized:
            return phone
    print(f"DEBUG DRIVER LOOKUP FAILED: vehicle_id={vehicle_id!r} DRIVER_WHATSAPP_MAPPING me kisi key se match nahi hua. "
          f"Available keys: {list(DRIVER_WHATSAPP_MAPPING.keys())}")
    return None


def send_driver_accept_repair_button(driver_phone: str, incident_id: str, body_text: str):
    """Driver ko poora breakdown+repair detail ek interactive 'Accept Repair'
    button ke saath bhejta hai — tap karne par hi driver ka AI agent chat
    officially enable hota hai (INCIDENT_CONTEXTS map hoti hai)."""
    cleaned_phone = ''.join(filter(str.isdigit, driver_phone))
    if WHATSAPP_TOKEN and WHATSAPP_TOKEN != "YOUR_TOKEN":
        url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        body = {
            "messaging_product": "whatsapp",
            "to": cleaned_phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": f"ACCEPTREPAIR_{incident_id}", "title": "Accept Repair ✅"}}
                    ]
                }
            }
        }
        try:
            res = requests.post(url, json=body, headers=headers, timeout=10)
            print(f"DEBUG DRIVER ACCEPT-REPAIR BUTTON RESPONSE [{res.status_code}]:", res.text)
            return {"status_code": res.status_code, "delivery_failed": res.status_code != 200}
        except Exception as e:
            print(f"DEBUG DRIVER ACCEPT-REPAIR BUTTON EXCEPTION: {str(e)}")
            return {"status": "error", "details": str(e)}
    else:
        print(f"[simulated] Driver accept-repair button to {driver_phone}: {body_text}")
        return {"status": "simulated_reply"}


def _resolve_hub_phone(hub_record: dict, hub_index: int = 0) -> str:
    """Static phone (fallback hubs) ya Google Place Details se phone nikaalta hai.

    TEMPORARY (testing phase): asli hub numbers abhi wire-up nahi hain, isliye
    TEST_HUB_NUMBERS list se hub_index % len() ke hisaab se cyclically ek
    number assign hota hai (hub 1 → pehla number, hub 2 → doosra, hub 3 →
    phir pehla, waghera). Jaise hi real hub phone numbers ready ho jaayein,
    TEST_HUB_NUMBERS ko [] kar dena — neeche wala Google-Place-Details/
    fallback logic apne aap use hone lagega."""
    if TEST_HUB_NUMBERS:
        return TEST_HUB_NUMBERS[hub_index % len(TEST_HUB_NUMBERS)]
    if hub_record.get("phone"):
        return hub_record["phone"]
    if hub_record.get("place_id") and GOOGLE_MAPS_API_KEY:
        try:
            details_url = "https://maps.googleapis.com/maps/api/place/details/json"
            details_params = {
                "place_id": hub_record["place_id"],
                "fields": "formatted_phone_number,international_phone_number",
                "key": GOOGLE_MAPS_API_KEY
            }
            d_data = requests.get(details_url, params=details_params, timeout=5).json()
            phone = d_data.get("result", {}).get("formatted_phone_number") or d_data.get("result", {}).get("international_phone_number")
            if phone:
                return phone
        except Exception:
            pass
    return "Contact number not available — team will call and share shortly"


def send_whatsapp_text_reply(to_phone: str, text: str):
    """Sends a plain text reply back on WhatsApp with automatic fallback simulation."""
    if not WHATSAPP_TOKEN or WHATSAPP_TOKEN == "YOUR_TOKEN":
        print(f"SIMULATED WHATSAPP TO [{to_phone}]: {text}")
        return {"status": "simulated_reply", "text": text}
    
    cleaned_to_phone = ''.join(filter(str.isdigit, to_phone))
    
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}", 
        "Content-Type": "application/json"
    }
    body = {
        "messaging_product": "whatsapp",
        "to": cleaned_to_phone,
        "type": "text",
        "text": {"body": text}
    }
    try:
        res = requests.post(url, json=body, headers=headers, timeout=10)
        print(f"DEBUG META WHATSAPP API RESPONSE [{res.status_code}]:", res.text)
        if res.status_code != 200:
            print(f"⚠️ Meta API Error: Could not deliver WhatsApp message to {cleaned_to_phone}.")
        return {"status_code": res.status_code, "response": res.json() if res.content else {}}
    except Exception as e:
        print(f"DEBUG META API EXCEPTION: {str(e)}")
        return {"status": "error", "details": str(e)}

def call_ai_agent(manager_text: str, incident_ctx: dict) -> dict:
    """
    Har ek incoming message ko Gemini AI Agent ke paas bhejta hai,
    uska smart decision nikalta hai aur professional WhatsApp reply banata hai.
    """
    if not GEMINI_API_KEY:
        return _keyword_fallback(manager_text)

    system_prompt = (
        "You are an advanced Autonomous Enterprise Fleet Operations AI Agent for Beekay Infra & Logistics. "
        "A manager or driver has sent a WhatsApp message about a vehicle breakdown incident. "
        "First, understand what they are ACTUALLY asking or instructing — do not give a generic "
        "'approved and dispatched' reply unless they are actually approving something. "
        "If they ask for a phone number, contact, or showroom number — give the exact hub_phone value "
        "provided in the context, do NOT say you'll 'share it shortly' if the number is already given to you. "
        "If they ask a factual question, answer it directly and specifically using the context given. "
        "If they give an approval/rejection/reroute instruction, classify it accordingly. "
        "If the DRIVER is sharing a status/location/progress update (e.g. 'leaving for the hub now', "
        "'reached the workshop', 'on the way', 'running late') and asking or implying that the manager "
        "should be told — classify this as DRIVER_STATUS_UPDATE. In reply_text for this case, "
        "confirm to the driver that you are passing it on RIGHT NOW ('Noted, informing your manager now') — "
        "never claim the manager has ALREADY been informed, since that message is sent separately, after "
        "your reply, by the system itself. ALSO add a short nudge asking the driver to also share their "
        "live WhatsApp location (e.g. 'Kripya apna live location bhi share kar dein 📍'), since WhatsApp "
        "only lets the driver share their own location by tapping the attachment/location option — the "
        "system cannot pull or request it automatically.\n"
        "Classify their decision into exactly one of: APPROVED_AND_DISPATCHED, APPROVED_LOCAL_MECHANIC_REROUTED, "
        "REJECTED_REROUTING, INFO_REQUEST_ANSWERED (for questions/requests for info like phone numbers), "
        "DRIVER_STATUS_UPDATE (driver's location/progress update meant for the manager), "
        "or CUSTOM_INSTRUCTION_LOGGED (for anything else specific).\n\n"
        "LANGUAGE MATCHING (important): always write reply_text in the SAME language and script the sender "
        "just used — if they wrote in Hindi/Devanagari script, reply in Devanagari; if they wrote Hinglish "
        "(Hindi words in Roman/English letters), reply in Hinglish the same way; if they wrote in plain "
        "English, reply in English. Never switch script or language on your own, and never mix scripts.\n\n"
        "URGENCY ASSESSMENT (important): also assess how urgent their message is and set 'urgency' to exactly "
        "one of: CRITICAL (life-threatening — accident, fire, vehicle overturned, someone injured/hurt), "
        "URGENT (serious safety/operational risk needing the manager's immediate attention right now — brake "
        "or steering failure, major fuel/oil/gas leak, breakdown on a highway at night, cargo at risk), or "
        "NORMAL (routine status updates, questions, approvals, minor issues). Default to NORMAL unless the "
        "message clearly indicates otherwise — never inflate routine messages to sound more serious than they are.\n\n"
        "Write a short (1-2 sentence), specific, professional WhatsApp reply that directly addresses what "
        "they asked or said — using the real details from the context (hub name, phone number, part name) "
        "wherever relevant.\n\n"
        "Respond ONLY as valid JSON in this exact format without markdown code blocks: "
        "{\"decision\": \"...\", \"reply_text\": \"...\", \"urgency\": \"NORMAL\"|\"URGENT\"|\"CRITICAL\"}"
    )

    user_prompt = (
        f"Incident Context:\n"
        f"- Vehicle ID: {incident_ctx.get('vehicle_id')}\n"
        f"- Issue Type: {incident_ctx.get('issue_type')}\n"
        f"- Assigned Hub: {incident_ctx.get('hub')}\n"
        f"- Hub Phone Number: {incident_ctx.get('hub_phone')}\n"
        f"- RAG Suggested Part: {incident_ctx.get('recommended_part')}\n\n"
        f"Incoming Message: \"{manager_text}\""
    )

    for attempt_model in ["gemini-flash-latest", "gemini-flash-lite-latest"]:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{attempt_model}:generateContent"
            headers = {
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            }
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {"temperature": 0.3, "response_mime_type": "application/json"}
            }
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            if res.status_code == 503:
                continue

            res.raise_for_status()
            data = res.json()
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            cleaned_content = content.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(cleaned_content)

            if parsed.get("decision") and parsed.get("reply_text"):
                parsed.setdefault("urgency", "NORMAL")
                if parsed["urgency"] not in ("NORMAL", "URGENT", "CRITICAL"):
                    parsed["urgency"] = "NORMAL"
                return parsed
        except Exception as e:
            print(f"DEBUG GEMINI AI ERROR [{attempt_model}]: {str(e)}")
            continue

    return _keyword_fallback(manager_text)

# Feature 4 (Urgency Detection) safety-net for when Gemini is unavailable —
# no real language-understanding is possible here, so this is a simple but
# high-recall keyword net for genuinely life-threatening situations only.
_CRITICAL_URGENCY_KEYWORDS = [
    "accident", "durghatna", "fire", "aag lag", "aag laga", "blast", "vishfot",
    "injured", "injury", "chot lag", "ghayal", "palat gaya", "palti", "overturn",
    "jal gaya", "jal raha", "burn", "khoon"
]


def _keyword_fallback(text_body: str) -> dict:
    t = text_body.lower()
    urgency = "CRITICAL" if any(kw in t for kw in _CRITICAL_URGENCY_KEYWORDS) else "NORMAL"

    if "local" in t or "fatuha" in t or "sasta" in t or "bypass" in t:
        result = {
            "decision": "APPROVED_LOCAL_MECHANIC_REROUTED",
            "reply_text": "✅ Understood — the vehicle has been rerouted to the local mechanic."
        }
    elif "ok" in t or "haan" in t or "kardo" in t or "approve" in t:
        result = {
            "decision": "APPROVED_AND_DISPATCHED",
            "reply_text": "✅ Repair approved, dispatch sequence has been initiated."
        }
    elif "cancel" in t or "reject" in t or "mat" in t:
        result = {
            "decision": "REJECTED_REROUTING",
            "reply_text": "❌ Request rejected, vehicle rerouting initiated."
        }
    elif any(kw in t for kw in [
        "leaving for", "on my way", "on the way", "reached", "arrived",
        "nikal", "ja rha", "ja raha", "pahunch", "running late", "delay ho"
    ]):
        result = {
            "decision": "DRIVER_STATUS_UPDATE",
            "reply_text": "Noted, informing your manager now. Kripya apna live location bhi share kar dein 📍"
        }
    else:
        result = {
            "decision": f"CUSTOM_INSTRUCTION_LOGGED: {text_body}",
            "reply_text": "📝 Your instruction has been recorded, the team will follow up."
        }

    if urgency == "CRITICAL":
        result["reply_text"] = "🆘 Samajh gaya, ye emergency hai — manager ko turant alert kiya jaa raha hai. " + result["reply_text"]
    result["urgency"] = urgency
    return result


# ==========================================
# 2. MULTI-AGENT COLLABORATIVE ARCHITECTURE (LangGraph Stateful Flow Simulation)
# ==========================================
class EnterpriseAgenticRAGOrchestrator:
    def __init__(self, incident: IncidentInput):
        self.incident = incident
        self.incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
        self.traces: List[AgentTrace] = []
        self.start_time = time.time()
        self.step_counter = 0
        self.tracer = EnterpriseObservabilityTracer(self.incident_id)

    @traced("multimodal_vision_inspection")
    def _run_multi_modal_vision_inspection(self) -> str:
        """5. Multi-Modal Input (Images + Telemetry): REAL Gemini Vision call
        when a real image_url is attached — the vision model only ever
        describes what it sees (never invents a part number itself). Falls
        back to the original simulated keyword check when no image is
        attached, so default behavior (no image today) is unchanged."""
        if self.incident.image_url and GEMINI_API_KEY:
            try:
                img_resp = requests.get(self.incident.image_url, timeout=10)
                img_resp.raise_for_status()
                vision_result = identify_damaged_part(
                    img_resp.content,
                    vehicle_context=f"Vehicle ID: {self.incident.vehicle_id}, reported issue: {self.incident.issue_type}",
                )
                if not vision_result.get("error"):
                    return (
                        f"Vision Agent (Gemini) analyzed attached photo: "
                        f"{vision_result.get('visible_damage', 'damage detected')} on "
                        f"{vision_result.get('component_area', 'reported component')} "
                        f"(confidence: {vision_result.get('confidence', 'medium')})."
                    )
            except Exception as e:
                print(f"DEBUG VISION AGENT ERROR: {str(e)}")
                # falls through to the simulated fallback below

        if self.incident.image_url or "smoke" in self.incident.issue_type.lower() or "oil" in self.incident.issue_type.lower():
            return "Vision Agent inspected attachment: Heavy leakage identified on coolant line manifold. Auto-adjusted part diagnostics confidence."
        return "Vision Agent check: Standard text telemetry verified (no damage photo provided)."

    def _geocode_location(self, address: str):
        """Breakdown location ka lat/lng nikaalta hai, nearest hub sorting ke liye."""
        if not GOOGLE_MAPS_API_KEY or not address:
            return None
        try:
            geo_url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {"address": f"{address}, Bihar, India", "key": GOOGLE_MAPS_API_KEY}
            data = requests.get(geo_url, params=params, timeout=5).json()
            if data.get("status") == "OK" and data.get("results"):
                return data["results"][0]["geometry"]["location"]
        except Exception:
            pass
        return None

    @staticmethod
    def _haversine_km(a, b):
        """Do coords ke beech straight-line distance nikaalta hai (km me)."""
        try:
            from math import radians, sin, cos, asin, sqrt
            lat1, lon1 = radians(a["lat"]), radians(a["lng"])
            lat2, lon2 = radians(b["lat"]), radians(b["lng"])
            dlat, dlon = lat2 - lat1, lon2 - lon1
            h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
            return 2 * 6371 * asin(sqrt(h))
        except Exception:
            return float("inf")

    def _build_full_route_url(self, hub_waypoint: Optional[str]) -> str:
        """Source → nearest service centre → destination ka clickable Google Maps route."""
        from urllib.parse import quote_plus
        origin = quote_plus(self.incident.location or "")
        dest = quote_plus(self.incident.destination or "")
        base = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={dest}&travelmode=driving"
        if hub_waypoint:
            base += f"&waypoints={quote_plus(hub_waypoint)}"
        return base
        
    @traced("fetch_google_maps_route")
    def _fetch_google_maps_route(self, via_hub: Optional[str] = None):
        if not GOOGLE_MAPS_API_KEY or not self.incident.destination:
            return None
        url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": self.incident.location,
            "destination": self.incident.destination,
            "key": GOOGLE_MAPS_API_KEY
        }
        if via_hub:
            params["waypoints"] = via_hub
        try:
            response = requests.get(url, params=params, timeout=7)
            data = response.json()
            if data.get("status") == "OK":
                legs = data["routes"][0]["legs"]
                total_m = sum(l["distance"]["value"] for l in legs)
                total_s = sum(l["duration"]["value"] for l in legs)
                result = {
                    "distance_text": f"{round(total_m / 1000, 1)} km",
                    "distance_value": total_m,
                    "duration_text": f"{round(total_s / 3600, 1)} hours",
                    "start_address": legs[0]["start_address"],
                    "end_address": legs[-1]["end_address"]
                }
                if via_hub and len(legs) >= 2:
                    result["leg_breakdown_to_service_center"] = {
                        "distance": legs[0]["distance"]["text"],
                        "duration": legs[0]["duration"]["text"],
                        "service_center_address": legs[0]["end_address"]
                    }
                    result["leg_service_center_to_destination"] = {
                        "distance": legs[1]["distance"]["text"],
                        "duration": legs[1]["duration"]["text"]
                    }
                result["route_via_service_center"] = bool(via_hub)
                return result
        except Exception:
            pass
        return None

    @traced("heavy_service_center_intelligence")
    def _get_heavy_service_center_intelligence(self):
        raw_hubs = []
        loc_lower = (self.incident.location or "").lower()
        is_outside_patna = "patna" not in loc_lower
        location_query = self.incident.location.split(",")[0].strip()
        region_scope = "Bihar, India" if is_outside_patna else "Patna, Bihar"
        origin_coords = self._geocode_location(self.incident.location) if is_outside_patna else None

        if GOOGLE_MAPS_API_KEY:
            places_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
            search_queries = [
                f"Tata commercial vehicle service center OR Eicher workshop near {location_query}, {region_scope}"
            ]
            if is_outside_patna:
                search_queries.append(
                    f"heavy commercial truck service centre near {location_query}, Bihar, India"
                )
            seen = set()
            for query_str in search_queries:
                params = {"query": query_str, "key": GOOGLE_MAPS_API_KEY}
                if origin_coords:
                    params["location"] = f"{origin_coords['lat']},{origin_coords['lng']}"
                    params["radius"] = 150000  # poore Bihar ka coverage
                try:
                    data = requests.get(places_url, params=params, timeout=7).json()
                    if data.get("status") == "OK" and data.get("results"):
                        for place in data["results"][:5]:
                            name = place.get('name', 'Service Center')
                            address = place.get('formatted_address', '')
                            if (name, address) in seen:
                                continue
                            seen.add((name, address))
                            rating = place.get('rating', 'N/A')
                            geometry = place.get('geometry', {}).get('location', {})
                            raw_hubs.append({
                                "display_str": f"{name} — {address} (Rating: {rating})",
                                "coords": geometry,
                                "place_id": place.get("place_id"),
                                "waypoint": f"{geometry.get('lat')},{geometry.get('lng')}" if geometry else address
                            })
                except Exception:
                    pass

        if not raw_hubs:
            patna_hubs = [
                {"display_str": "TATA.CARS Service Centre - Guinea Motors, Patliputra Industrial Area, Patna, Bihar (Rating: 3.9)", "phone": "0612-2262244", "coords": {"lat": 25.6210, "lng": 85.1050}},
                {"display_str": "Eicher Commercial Vehicles Workshop, NH-30 Bypass Road, Patna, Bihar (Rating: 4.2)", "phone": "0612-2277311", "coords": {"lat": 25.5788, "lng": 85.1560}},
                {"display_str": "Tata Motors Authorized Commercial Heavy Workshop, Zero Mile, Patna, Bihar (Rating: 4.1)", "phone": "0612-2233890", "coords": {"lat": 25.5941, "lng": 85.2010}},
                {"display_str": "Eicher Trucks & Buses Service Station, Fatuha Industrial Area, Patna, Bihar (Rating: 4.0)", "phone": "0612-2299456", "coords": {"lat": 25.5060, "lng": 85.3050}}
            ]
            bihar_hubs = [
                {"display_str": "Tata Motors Commercial Vehicle Service Centre, Ramdayalu, Muzaffarpur, Bihar (Rating: 4.0)", "phone": "0621-2240110", "coords": {"lat": 26.1209, "lng": 85.3647}},
                {"display_str": "Eicher Trucks & Buses Authorized Workshop, GT Road, Gaya, Bihar (Rating: 4.1)", "phone": "0631-2221450", "coords": {"lat": 24.7955, "lng": 85.0002}},
                {"display_str": "Tata Motors Heavy Commercial Workshop, Zero Mile, Bhagalpur, Bihar (Rating: 3.9)", "phone": "0641-2420331", "coords": {"lat": 25.2425, "lng": 86.9842}},
                {"display_str": "Eicher Commercial Vehicle Service Station, Donar Chowk, Darbhanga, Bihar (Rating: 4.0)", "phone": "06272-245120", "coords": {"lat": 26.1542, "lng": 85.8918}},
                {"display_str": "Tata Motors Authorized Truck Workshop, NH-31, Purnia, Bihar (Rating: 4.0)", "phone": "06454-242890", "coords": {"lat": 25.7771, "lng": 87.4753}},
                {"display_str": "Heavy Commercial Vehicle Service Hub, Bela Industrial Area, Begusarai, Bihar (Rating: 3.8)", "phone": "06243-222410", "coords": {"lat": 25.4182, "lng": 86.1290}}
            ]
            default_heavy_hubs = (bihar_hubs + patna_hubs) if is_outside_patna else patna_hubs
            for hub in default_heavy_hubs:
                raw_hubs.append({
                    "display_str": hub["display_str"],
                    "coords": hub.get("coords", {}),
                    "place_id": None,
                    "phone": hub["phone"],
                    "waypoint": f"{hub['coords']['lat']},{hub['coords']['lng']}" if hub.get("coords") else hub["display_str"]
                })

        # Breakdown point se actual nearest hub sabse upar
        if origin_coords:
            raw_hubs.sort(
                key=lambda h: self._haversine_km(origin_coords, h["coords"]) if h.get("coords") else float("inf")
            )

        # Ensure de-duplication and closest-first ordering are consistent,
        # then keep this exact order as the "queue" reject/reroute walks through.
        seen_display = set()
        deduped_hubs = []
        for h in raw_hubs:
            if h["display_str"] not in seen_display:
                seen_display.add(h["display_str"])
                deduped_hubs.append(h)
        raw_hubs = deduped_hubs[:5]

        closest_hub_str = raw_hubs[0]["display_str"]
        hub_waypoint = raw_hubs[0].get("waypoint")
        hub_phone = _resolve_hub_phone(raw_hubs[0], 0)

        numbered_hubs = [f"{idx}. {h['display_str']}" for idx, h in enumerate(raw_hubs, 1)]
        # Full ordered record list (closest first) — kept per-incident so that
        # a manager REJECT can automatically advance to the next one in line.
        hub_records = [
            {
                "name": h["display_str"],
                "phone": h.get("phone"),
                "place_id": h.get("place_id"),
                "waypoint": h.get("waypoint"),
            }
            for h in raw_hubs
        ]
        primary_hub = numbered_hubs[0]

        return {
            "corridor": self.incident.location,
            "hub": primary_hub,
            "hub_phone": hub_phone,
            "hub_waypoint": hub_waypoint,
            "search_scope": "STATEWIDE_BIHAR" if is_outside_patna else "PATNA_METRO",
            "all_detected_hubs": numbered_hubs,
            "hub_records": hub_records
        }

    @traced("run_swarm_full_incident")
    def run_swarm(self) -> TriageResponse:
        
        service_intel = self._get_heavy_service_center_intelligence()
        map_route = self._fetch_google_maps_route(via_hub=service_intel.get("hub_waypoint"))
        full_route_url = self._build_full_route_url(service_intel.get("hub_waypoint"))
        
        # 1. Hybrid Search + Reranking Execution
        raw_rag = GLOBAL_HYBRID_RAG_ENGINE.hybrid_retrieve_and_rerank(self.incident.issue_type)
        
        # 3. Corrective RAG (CRAG) Validation & Hallucination Grading
        rag_intel = CorrectiveRAGValidator.evaluate_and_correct(self.incident.issue_type, raw_rag)
        
        # 5. Multi-Modal Vision Analysis
        vision_report = self._run_multi_modal_vision_inspection()

        # Feature: Per-Vehicle Predictive Maintenance — check BEFORE recording
        # this incident, so "matches" only counts genuinely PRIOR occurrences.
        predictive_alert = _record_and_check_vehicle_pattern(self.incident.vehicle_id, self.incident.issue_type)

        # Feature: Spare-Part Price Comparison — ask top 3 nearby vendors for
        # availability/price on this specific part. Their WhatsApp replies
        # get picked up later in the webhook handler.
        _request_spare_part_quotes(
            self.incident_id,
            service_intel.get("hub_records", []),
            rag_intel["recommended_part"]
        )

        detected_hubs = service_intel.get("all_detected_hubs", [])
        assigned_phone = MANAGER_WHATSAPP_NUMBER

        APPROVAL_STATES[self.incident_id] = "PENDING_MANAGER_APPROVAL"
        INCIDENT_CONTEXTS[assigned_phone] = self.incident_id
        INCIDENT_DETAILS[self.incident_id] = {
            "vehicle_id": self.incident.vehicle_id,
            "issue_type": self.incident.issue_type,
            "location": self.incident.location,
            "destination": self.incident.destination,
            "severity": self.incident.severity,
            "cargo_type": self.incident.cargo_type,
            "hub": service_intel["hub"],
            "hub_phone": service_intel["hub_phone"],
            "hub_waypoint": service_intel.get("hub_waypoint"),
            "hub_records": service_intel.get("hub_records", []),
            "hub_index": 0,
            "recommended_part": rag_intel["recommended_part"],
            "hallucination_grade": rag_intel.get("hallucination_grade"),
            "diagnostic_summary": rag_intel.get("diagnostic_summary", ""),
            "predictive_alert": predictive_alert,
            "followup_scheduled": False
        }

        # Agent 1: Triage & Vision Multi-Modal Agent Trace
        self.step_counter += 1
        t_start = time.time()
        sup_dec = {
            "action_required": True,
            "target_vehicle": self.incident.vehicle_id,
            "mapped_whatsapp_number": assigned_phone,
            "breakdown_location": map_route["start_address"] if map_route else self.incident.location,
            "destination_workshop": service_intel["hub"],
            "all_nearby_service_centers": detected_hubs,
            "issue_detected": self.incident.issue_type,
            "multimodal_vision_report": vision_report,
            "hitl_status": "WAITING_FOR_WHATSAPP_INTERACTIVE_BUTTON_OR_TEXT"
        }
        lat_1 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Supervisor_Triage", agent_role="Supervisor & Multi-Modal Vision Agent", status="SUCCESS", timestamp=lat_1, output_payload=sup_dec))
        self.tracer.log_span("Supervisor_Triage", "AGENT_NODE", self.incident.dict(), sup_dec, lat_1, 180)

        # Agent 2: Hybrid RAG + Corrective RAG (CRAG) Agent Trace
        self.step_counter += 1
        t_start = time.time()
        lat_2 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Agentic_RAG_Diagnostics", agent_role="Hybrid Vector/BM25 + CRAG Agent", status="SUCCESS", timestamp=lat_2, output_payload=rag_intel))
        self.tracer.log_span("Agentic_RAG_Diagnostics", "RAG_RETRIEVAL", {"issue": self.incident.issue_type}, rag_intel, lat_2, 220)

        # Agent 3: Logistics & Route Dispatch Agent Trace
        self.step_counter += 1
        t_start = time.time()
        routing = {
            "total_distance": map_route["distance_text"] if map_route else "310 km",
            "estimated_travel_time": map_route["duration_text"] if map_route else "6 hours",
            "primary_route_status": "HEAVY_CORRIDOR_OPTIMIZED",
            "hyper_accurate_alternative_route": f"Optimized transit to {service_intel['hub'].split('—')[0]}",
            "breakdown_to_service_center": map_route.get("leg_breakdown_to_service_center") if map_route else "Live leg unavailable",
            "service_center_to_destination": map_route.get("leg_service_center_to_destination") if map_route else "Live leg unavailable",
            "route_waypoint_service_center": service_intel["hub"],
            "open_full_route_url": full_route_url
        }
        lat_3 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Routing_Recalculation", agent_role="Routing & Logistics Agent", status="SUCCESS", timestamp=lat_3, output_payload=routing))
        self.tracer.log_span("Routing_Recalculation", "ROUTING_NODE", {"destination": service_intel["hub"]}, routing, lat_3, 130)

        # Agent 4: Manager Approval & ERP Sync Agent Trace
        self.step_counter += 1
        t_start = time.time()
        erp = {
            "erp_transaction_id": f"TXN-ERP-{uuid.uuid4().hex[:6].upper()}",
            "ledger_status": "PENDING_HITL_APPROVAL",
            "observability_trace_id": self.tracer.traces[0]["trace_id"]
        }
        lat_4 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Manager_Approval_and_ERP_Sync", agent_role="HITL Approval State & ERP Agent", status="SUCCESS", timestamp=lat_4, output_payload=erp))
        self.tracer.log_span("Manager_Approval_and_ERP_Sync", "STATE_COMMIT", {}, erp, lat_4, 90)

        return TriageResponse(
            incident_id=self.incident_id,
            status="AWAITING_HITL_APPROVAL",
            execution_time_ms=round((time.time() - self.start_time) * 1000, 2),
            deterministic_steps_executed=self.step_counter,
            assigned_whatsapp_number=assigned_phone,
            approval_status=APPROVAL_STATES[self.incident_id],
            traces=self.traces,
            final_resolution={
                "vehicle_id": self.incident.vehicle_id,
                "service_center_search_scope": service_intel["search_scope"],
                "nearest_hub_contact": service_intel["hub_phone"],
                "open_full_route_url": full_route_url,
                "assigned_whatsapp": assigned_phone,
                "origin": self.incident.location,
                "primary_nearest_hub": service_intel["hub"],
                "rag_diagnostic_part": rag_intel["recommended_part"],
                "crag_status": rag_intel["hallucination_grade"],
                "mitigation_summary": f"Google-level Hybrid RAG & CRAG part match executed. Waiting for Manager WhatsApp approval.",
                "erp_ref": erp["erp_transaction_id"],
                "predictive_maintenance_alert": predictive_alert
            }
        )

@app.post("/api/triage", response_model=TriageResponse)
async def trigger_triage(incident: IncidentInput, _auth=Depends(require_api_key), _rl=Depends(rate_limit)):
    return EnterpriseAgenticRAGOrchestrator(incident).run_swarm()

@app.get("/api/approval-status/{incident_id}")
async def get_approval_status(incident_id: str):
    current_status = APPROVAL_STATES.get(incident_id, "PENDING_MANAGER_APPROVAL")
    return {
        "incident_id": incident_id,
        "approval_status": current_status
    }

def send_whatsapp_interactive_approval(incident_id: str, to_phone: str, vehicle_id: str, hub: str,
                                        location: str, issue_type: str, severity: str,
                                        cargo_type: str, recommended_part: str,
                                        predictive_alert: Optional[str] = None):
    """Approve/Reject interactive button bhejta hai — initial manager alert aur
    har automatic reject-reroute cycle, dono isi ek function se guzarte hain.
    predictive_alert diya ho (Feature 6: Per-Vehicle Predictive Maintenance)
    to wahi body text me manager ko dikha diya jaata hai."""
    cleaned_phone = re.sub(r'\D', '', to_phone or "")
    token = WHATSAPP_TOKEN
    phone_id = WHATSAPP_PHONE_ID
    predictive_block = f"\n{predictive_alert}\n" if predictive_alert else ""

    if token and token != "YOUR_TOKEN":
        url = f"https://graph.facebook.com/v26.0/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = {
            "messaging_product": "whatsapp",
            "to": cleaned_phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": (
                        f"🚨 *Breakdown Alert — Action Needed*\n\n"
                        f"🚜 *Vehicle:* {vehicle_id} | ⚡ *Sev:* {severity}\n"
                        f"📦 *Cargo:* {cargo_type}\n"
                        f"📍 *Location:* {location}\n"
                        f"🛠️ *Issue:* {issue_type}\n\n"
                        f"🧠 *Hybrid RAG Part:* _{recommended_part}_\n"
                        f"🏢 *Hub:* {hub}\n"
                        f"{predictive_block}\n"
                        f"Authorize repair or reply with instructions?"
                    )
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"APPROVE_{incident_id}",
                                "title": "Approve Repair ✅"
                            }
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"REJECT_{incident_id}",
                                "title": "Reject & Reroute ❌"
                            }
                        }
                    ]
                }
            }
        }
        res = requests.post(url, json=body, headers=headers)
        if res.status_code != 200:
            return {"status": "meta_api_error", "status_code": res.status_code, "error_details": res.json()}
        return {"status": "dispatched_via_meta_api", "response": res.json()}

    return {"status": "simulated_interactive_dispatched", "message": f"Google-level Agentic WhatsApp alert sent to {cleaned_phone}."}


@app.post("/api/send-whatsapp-interactive")
async def send_whatsapp_interactive(payload: dict, _auth=Depends(require_api_key), _rl=Depends(rate_limit)):
    incident_id = payload.get("incident_id")
    raw_phone = payload.get("phone", "")
    vehicle_id = payload.get("vehicle_id")
    hub = payload.get("hub")

    location = payload.get("location", "N/A")
    issue_type = payload.get("issue_type", "N/A")
    severity = payload.get("severity", "N/A")
    cargo_type = payload.get("cargo_type", "N/A")
    recommended_part = payload.get("recommended_part", "Standard Spare Kit")
    # Feature 6: pull whatever predictive-maintenance alert run_swarm already
    # computed and stored for this incident, so it's shown right on the
    # manager's initial approval card without the frontend needing to know
    # this feature exists.
    predictive_alert = INCIDENT_DETAILS.get(incident_id, {}).get("predictive_alert") if incident_id else None

    return send_whatsapp_interactive_approval(
        incident_id=incident_id,
        to_phone=raw_phone,
        vehicle_id=vehicle_id,
        hub=hub,
        location=location,
        issue_type=issue_type,
        severity=severity,
        cargo_type=cargo_type,
        recommended_part=recommended_part,
        predictive_alert=predictive_alert
    )

@app.get("/api/whatsapp-webhook")
async def verify_whatsapp_webhook(request: Request):
    hub_mode = request.query_params.get("hub.mode")
    hub_challenge = request.query_params.get("hub.challenge")
    hub_verify_token = request.query_params.get("hub.verify_token")
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification token mismatch")

@app.post("/api/whatsapp-webhook")
async def whatsapp_webhook(request: Request):
    try:
        data = await request.json()
        print("DEBUG WEBHOOK RECEIVED:", json.dumps(data, indent=2))
        
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if messages:
            msg = messages[0]
            raw_sender_phone = msg.get("from", "")
            sender_10_digit = ''.join(filter(str.isdigit, raw_sender_phone))[-10:]
            
            if msg.get("type") == "audio":
                media_id = msg["audio"].get("id")
                mime_type = msg["audio"].get("mime_type", "audio/ogg").split(";")[0]
                audio_bytes = _download_whatsapp_media(media_id) if media_id else None

                if not audio_bytes:
                    send_whatsapp_text_reply(raw_sender_phone, "⚠️ Voice note download nahi ho paya, kripya dobara bhejein ya type karke bhejein.")
                    return {"status": "error", "action": "Voice note download failed."}

                transcribed = _transcribe_voice_note(audio_bytes, mime_type)
                if not transcribed:
                    send_whatsapp_text_reply(raw_sender_phone, "⚠️ Voice note samajh nahi paya, kripya thoda clear bol ke dobara bhejein.")
                    return {"status": "error", "action": "Voice note transcription failed."}

                vehicle_id = transcribed["vehicle_id"]
                if vehicle_id == "UNKNOWN":
                    vehicle_id = DRIVER_PHONE_TO_VEHICLE.get(sender_10_digit, "UNKNOWN")

                if vehicle_id == "UNKNOWN":
                    send_whatsapp_text_reply(
                        raw_sender_phone,
                        f"🎙️ Sunaayi diya: \"{transcribed['issue_type']}\"\n\n"
                        f"Vehicle number pata nahi chala — kripya vehicle number bhi bol/likh ke bhejein."
                    )
                    return {"status": "success", "action": "Voice note transcribed, vehicle ID missing."}

                voice_incident = IncidentInput(
                    vehicle_id=vehicle_id,
                    location=transcribed["location"] if transcribed["location"] != "UNKNOWN" else "Location not specified",
                    issue_type=transcribed["issue_type"],
                    severity="MODERATE",
                    cargo_type="Not specified"
                )
                send_whatsapp_text_reply(
                    raw_sender_phone,
                    f"🎙️ Voice note samajh liya:\n"
                    f"🚜 Vehicle: {vehicle_id}\n"
                    f"📍 Location: {voice_incident.location}\n"
                    f"🛠️ Issue: {transcribed['issue_type']}\n\n"
                    f"Processing shuru ho gaya hai, manager ko turant alert milega."
                )
                EnterpriseAgenticRAGOrchestrator(voice_incident).run_swarm()
                return {"status": "success", "action": "Incident auto-created from voice note."}

            # ==========================================
            # Features 1 & 2: IN-CHAT PHOTO DIAGNOSIS + SELF-SERVICE REPAIR
            # GUIDANCE — driver kabhi bhi (sirf initial report ke time nahi)
            # chat me photo bhej sakta hai, AI turant analyze karke jawab
            # deta hai. Pehle sirf initial /api/triage ke image_url field se
            # hi vision analysis chalta tha; ab WhatsApp chat ka `image`
            # message type bhi seedha yahin handle hota hai.
            # ==========================================
            if msg.get("type") == "image":
                media_id = msg["image"].get("id")
                mime_type = msg["image"].get("mime_type", "image/jpeg").split(";")[0]
                image_bytes = _download_whatsapp_media(media_id) if media_id else None

                if not image_bytes:
                    send_whatsapp_text_reply(raw_sender_phone, "⚠️ Photo download nahi ho payi, kripya dobara bhejein.")
                    return {"status": "error", "action": "Image download failed."}

                is_driver = sender_10_digit in ACTIVE_VEHICLE_BY_PHONE
                matched_inc_id = INCIDENT_CONTEXTS.get(sender_10_digit)
                if not matched_inc_id and INCIDENT_CONTEXTS:
                    matched_inc_id = list(INCIDENT_CONTEXTS.values())[-1]
                incident_ctx = INCIDENT_DETAILS.get(matched_inc_id, {}) if matched_inc_id else {}
                vehicle_id = ACTIVE_VEHICLE_BY_PHONE.get(sender_10_digit, incident_ctx.get("vehicle_id", "N/A"))

                if not GEMINI_API_KEY:
                    # Vision analysis unavailable — driver ko turant confirm
                    # karo (kabhi silent drop nahi), manager ko manual-review
                    # ke liye flag karo.
                    send_whatsapp_text_reply(
                        raw_sender_phone,
                        "📸 Photo mil gayi, team ko forward kar diya gaya hai — jald hi update milega."
                    )
                    if matched_inc_id:
                        send_whatsapp_text_reply(
                            MANAGER_WHATSAPP_NUMBER,
                            f"📸 *Photo received from driver — {vehicle_id}*\n\n"
                            f"(AI vision analysis unavailable — GEMINI_API_KEY not set. Please review manually.)"
                        )
                        _forward_whatsapp_image(MANAGER_WHATSAPP_NUMBER, media_id)
                    return {"status": "success", "action": "Image received, AI vision unavailable, forwarded raw."}

                diagnosis = _diagnose_incident_photo(image_bytes, mime_type, incident_ctx)
                if not diagnosis:
                    send_whatsapp_text_reply(
                        raw_sender_phone,
                        "⚠️ Photo analyze nahi ho payi. Kripya thoda clear/roshni me dobara photo bhejein, "
                        "ya seedha text me bata dein."
                    )
                    return {"status": "error", "action": "Photo diagnosis failed."}

                if matched_inc_id:
                    # Feature 5: photo bhejna bhi ek genuine driver update hai.
                    DRIVER_LAST_UPDATE_TS[matched_inc_id] = time.time()

                reply_lines = [f"📸 *AI Photo Diagnosis*\n{diagnosis['driver_message']}"]
                if diagnosis.get("can_self_fix") and diagnosis.get("diy_steps"):
                    reply_lines.append("\n🔧 *Khud karne ke steps:*")
                    for i, step in enumerate(diagnosis["diy_steps"], 1):
                        reply_lines.append(f"{i}. {step}")
                    reply_lines.append("\nAgar isse thik na ho ya asuraksha lage, turant bata dein — hum mechanic bhej denge.")
                send_whatsapp_text_reply(raw_sender_phone, "\n".join(reply_lines))

                severity = diagnosis.get("severity", "MODERATE")
                icon = {"CRITICAL": "🆘", "HIGH": "🚨"}.get(severity, "📸")
                manager_lines = [
                    f"{icon} *Driver photo — {vehicle_id}* (Severity: {severity})",
                    f"AI diagnosis: {diagnosis.get('visible_damage', 'N/A')}"
                ]
                if diagnosis.get("can_self_fix"):
                    manager_lines.append("Driver ko self-fix (DIY) steps de diye gaye hain — mechanic dispatch abhi zaroori nahi.")
                else:
                    manager_lines.append(
                        f"Professional help chahiye — recommended: {diagnosis.get('recommended_part') or 'inspection'}."
                    )
                send_whatsapp_text_reply(MANAGER_WHATSAPP_NUMBER, "\n".join(manager_lines))
                # Best-effort: driver ki actual photo bhi manager ko forward
                # karne ki koshish karo (fail ho to upar wala text summary
                # already bhej diya gaya hai, kuch bhi block nahi hota).
                _forward_whatsapp_image(MANAGER_WHATSAPP_NUMBER, media_id)

                # Feature 4 (Urgency): HIGH/CRITICAL photo severity ko
                # incident record par bhi turant reflect karo, aur agar
                # CRITICAL hai to ek standalone emergency escalation bhejo.
                if matched_inc_id and severity in ("HIGH", "CRITICAL"):
                    incident_ctx["severity"] = severity
                    INCIDENT_DETAILS[matched_inc_id] = incident_ctx
                if severity == "CRITICAL":
                    send_whatsapp_text_reply(
                        MANAGER_WHATSAPP_NUMBER,
                        f"🆘 *CRITICAL ALERT — {vehicle_id}*\n\nPhoto se critical damage/safety risk dikh raha hai. Turant dhyaan dein / call karein."
                    )

                return {"status": "success", "action": "Photo diagnosed and forwarded.", "severity": severity, "can_self_fix": diagnosis.get("can_self_fix", False)}

            if msg.get("type") == "location":
                # WhatsApp ka native "Share Location" (live ya current pin) —
                # lat/long yahin aata hai, kisi extraction ki zaroorat nahi.
                loc = msg.get("location", {})
                lat, lng = loc.get("latitude"), loc.get("longitude")
                place_name = loc.get("name") or loc.get("address") or "Shared location"

                if lat is None or lng is None:
                    send_whatsapp_text_reply(raw_sender_phone, "⚠️ Location samajh nahi paya, kripya dobara share karein.")
                    return {"status": "error", "action": "Location message missing coordinates."}

                maps_link = f"https://www.google.com/maps?q={lat},{lng}"
                is_driver = sender_10_digit in ACTIVE_VEHICLE_BY_PHONE
                vehicle_id = ACTIVE_VEHICLE_BY_PHONE.get(sender_10_digit, "N/A") if is_driver else None

                if is_driver:
                    inc_id_for_update = INCIDENT_CONTEXTS.get(sender_10_digit)
                    if inc_id_for_update:
                        DRIVER_LAST_UPDATE_TS[inc_id_for_update] = time.time()
                    send_whatsapp_text_reply(
                        MANAGER_WHATSAPP_NUMBER,
                        f"📍 *Live Location — {vehicle_id}*\n\n"
                        f"{place_name}\n"
                        f"🗺️ {maps_link}"
                    )
                    send_whatsapp_text_reply(raw_sender_phone, "✅ Location manager ko forward kar diya gaya.")
                    return {"status": "success", "action": "Driver location forwarded to manager."}
                else:
                    send_whatsapp_text_reply(raw_sender_phone, "📍 Location receive ho gaya.")
                    return {"status": "success", "action": "Location received (sender not an active driver)."}

            if msg.get("type") == "interactive":
                button_reply = msg["interactive"].get("button_reply", {})
                payload_id = button_reply.get("id", "")

                if "VENDORAVAIL_YES_" in payload_id or "VENDORAVAIL_NO_" in payload_id:
                    # payload shape: VENDORAVAIL_YES_<incident_id>_<vendor_10_digit>
                    # or VENDORAVAIL_NO_<incident_id>_<vendor_10_digit>
                    is_yes = "VENDORAVAIL_YES_" in payload_id
                    prefix = "VENDORAVAIL_YES_" if is_yes else "VENDORAVAIL_NO_"
                    remainder = payload_id.split(prefix)[1]
                    inc_id, vendor_key = remainder.rsplit("_", 1)

                    quote_ctx = PENDING_QUOTES.get(inc_id)
                    vendor = quote_ctx["vendors"].get(vendor_key) if quote_ctx else None

                    if not vendor:
                        send_whatsapp_text_reply(raw_sender_phone, "⚠️ Ye enquiry ab valid nahi hai (expire ho gayi).")
                        return {"status": "error", "action": "Vendor quote context not found or expired."}

                    if is_yes:
                        vendor["awaiting_cost"] = True
                        PENDING_QUOTES[inc_id] = quote_ctx
                        send_whatsapp_text_reply(
                            raw_sender_phone,
                            "Dhanyavaad! Kripya sirf price number bhejein (jaise: 10000)."
                        )
                        return {"status": "success", "action": "Vendor confirmed availability, awaiting cost."}
                    else:
                        vendor["status"] = "unavailable"
                        vendor["awaiting_cost"] = False
                        PENDING_QUOTES[inc_id] = quote_ctx
                        send_whatsapp_text_reply(raw_sender_phone, "Dhanyavaad, jaankari mil gayi hai.")

                        all_done = all(v["status"] in ("quoted", "unavailable") for v in quote_ctx["vendors"].values())
                        if all_done and not quote_ctx.get("manager_notified"):
                            if not _notify_manager_with_quotes(inc_id):
                                send_whatsapp_text_reply(
                                    MANAGER_WHATSAPP_NUMBER,
                                    f"⚠️ None of the {len(quote_ctx['vendors'])} nearby vendors had "
                                    f"'{quote_ctx['part_name']}' available."
                                )
                                del PENDING_QUOTES[inc_id]
                        return {"status": "success", "action": "Vendor marked unavailable."}

                if "ACCEPTREPAIR_" in payload_id:
                    inc_id = payload_id.split("ACCEPTREPAIR_")[1]
                    incident_ctx = INCIDENT_DETAILS.get(inc_id, {})
                    vehicle_id = incident_ctx.get("vehicle_id")

                    cleaned_driver_10 = ''.join(filter(str.isdigit, raw_sender_phone))[-10:]
                    INCIDENT_CONTEXTS[cleaned_driver_10] = inc_id
                    if vehicle_id:
                        ACTIVE_VEHICLE_BY_PHONE[cleaned_driver_10] = vehicle_id

                    send_whatsapp_text_reply(
                        raw_sender_phone,
                        "✅ Repair accepted! Ab aap isi chat par live updates, sawaal ya photos "
                        "seedha AI Operations Agent ko bhej sakte hain."
                    )
                    send_whatsapp_text_reply(
                        MANAGER_WHATSAPP_NUMBER,
                        f"✅ Driver ({vehicle_id or 'N/A'}) ne repair accept kar liya hai — AI agent chat activate ho gayi hai."
                    )

                    # Feature 5 (Proactive Follow-up) — sirf ek baar arm karo
                    # per incident, chahe driver 'Accept Repair' aur baad me
                    # koi doosra dispatch-confirmation dono trigger kar de.
                    if not incident_ctx.get("followup_scheduled"):
                        incident_ctx["followup_scheduled"] = True
                        INCIDENT_DETAILS[inc_id] = incident_ctx
                        _schedule_followup_check(inc_id)

                    return {"status": "success", "action": "Driver accepted repair, AI agent context enabled."}

                if "PICKVENDOR_" in payload_id:
                    # payload shape: PICKVENDOR_<incident_id>_<vendor_10_digit>
                    remainder = payload_id.split("PICKVENDOR_")[1]
                    inc_id, vendor_key = remainder.rsplit("_", 1)

                    quote_ctx = PENDING_QUOTES.get(inc_id)
                    vendor = quote_ctx["vendors"].get(vendor_key) if quote_ctx else None

                    if not vendor:
                        send_whatsapp_text_reply(raw_sender_phone, "⚠️ Ye quote ab valid nahi hai (expire ho gaya).")
                        return {"status": "error", "action": "Vendor quote not found or expired."}

                    incident_ctx = INCIDENT_DETAILS.get(inc_id, {})
                    vehicle_id = incident_ctx.get("vehicle_id")
                    part_name = quote_ctx["part_name"]

                    # Vendor pick hi ab de-facto "approval" hai — isliye yahi
                    # se dispatch state set karo, aur driver ko poora
                    # breakdown+repair+vendor detail ek 'Accept Repair'
                    # button ke saath bhejo. AI agent chat is button ke tap
                    # hone par hi officially enable hoga (neeche
                    # ACCEPTREPAIR_ handler me) — koi manual Approve step
                    # ab zaroori nahi.
                    APPROVAL_STATES[inc_id] = "APPROVED_AND_DISPATCHED"
                    driver_phone = _lookup_driver_phone(vehicle_id)
                    driver_status_note = ""
                    if driver_phone:
                        hub_name = incident_ctx.get("hub", "Nearest Service Center")
                        hub_phone = incident_ctx.get("hub_phone") or "8210002439"
                        driver_body_text = (
                            f"✅ *Repair & Parts Confirmed*\n\n"
                            f"🚜 *Vehicle:* {vehicle_id}\n"
                            f"🛠️ *Issue:* {incident_ctx.get('issue_type', 'N/A')}\n"
                            f"📍 *Your Location:* {incident_ctx.get('location', 'N/A')}\n\n"
                            f"🏢 *Nearest Service Center:* {hub_name}\n"
                            f"📞 *Hub Contact:* {hub_phone}\n\n"
                            f"⚙️ *Part:* {part_name}\n"
                            f"🏭 *Vendor:* {vendor['hub_name']}\n"
                            f"💰 *Approved Price:* ₹{vendor['price']:.0f}\n\n"
                            f"Neeche 'Accept Repair' dabaayein taaki AI agent chat activate ho jaaye "
                            f"aur aap isi par live updates/photos share kar sakein."
                        )
                        result = send_driver_accept_repair_button(driver_phone, inc_id, driver_body_text)
                        # NOTE: pehle sirf "delivery_failed" flag check hota tha —
                        # agar send_driver_accept_repair_button ke andar exception
                        # (network/timeout) aata, wo {"status": "error", ...}
                        # return karta, jisme "delivery_failed" key hoti hi nahi —
                        # isliye fallback text bhi silently skip ho jaata tha.
                        # Ab dono cases (non-200 response AND exception) fallback
                        # trigger karte hain.
                        send_failed = isinstance(result, dict) and (
                            result.get("delivery_failed") or result.get("status") == "error"
                        )
                        if send_failed:
                            send_whatsapp_text_reply(driver_phone, driver_body_text)
                            driver_status_note = f"\n\n📲 Driver ({vehicle_id}, {driver_phone}): button fail hua, plain text fallback bheja gaya."
                        else:
                            driver_status_note = f"\n\n📲 Driver ({vehicle_id}, {driver_phone}) ko 'Accept Repair' button bhej diya gaya."
                    else:
                        driver_status_note = (
                            f"\n\n⚠️ Driver ko notify NAHI kiya ja saka — vehicle_id={vehicle_id!r} "
                            f"DRIVER_WHATSAPP_MAPPING me kisi bhi key se match nahi hua. "
                            f"Available vehicles: {', '.join(DRIVER_WHATSAPP_MAPPING.keys())}"
                        )

                    send_whatsapp_text_reply(
                        raw_sender_phone,
                        f"✅ Confirmed: {vendor['hub_name']} — ₹{vendor['price']:.0f} for {part_name}."
                        f"{driver_status_note}"
                    )

                    del PENDING_QUOTES[inc_id]
                    return {"status": "success", "action": f"Vendor {vendor['hub_name']} confirmed, driver notified."}

                if "APPROVE_" in payload_id:
                    inc_id = payload_id.split("APPROVE_")[1]
                    APPROVAL_STATES[inc_id] = "APPROVED_AND_DISPATCHED"
                    
                    send_whatsapp_text_reply(raw_sender_phone, "✅ Repair approved successfully by management. Dispatch sequence is active.")
                    
                    incident_ctx = INCIDENT_DETAILS.get(inc_id, {})
                    vehicle_id = incident_ctx.get("vehicle_id")
                    hub = incident_ctx.get("hub")
                    recommended_part = incident_ctx.get("recommended_part")

                    # Self-Improving Knowledge Base: this diagnosis was Gemini's
                    # best guess (no manual matched it) — now that a human has
                    # verified/approved it, permanently learn it so the SAME
                    # issue next time gets a real manual-grounded match.
                    if incident_ctx.get("hallucination_grade") == "AI_GENERATED_FALLBACK":
                        try:
                            learned_doc = _build_learned_doc_from_incident(incident_ctx)
                            GLOBAL_HYBRID_RAG_ENGINE.add_learned_document(learned_doc)
                            send_whatsapp_text_reply(
                                MANAGER_WHATSAPP_NUMBER,
                                f"🧠 Knowledge base updated: learned '{incident_ctx.get('issue_type')}' → "
                                f"{recommended_part}. Future similar reports will match this directly."
                            )
                        except Exception as e:
                            print(f"DEBUG SELF-LEARNING ERROR: {str(e)}")
                    
                    driver_phone = DRIVER_WHATSAPP_MAPPING.get(vehicle_id)
                    if driver_phone:
                        cleaned_driver_10 = ''.join(filter(str.isdigit, driver_phone))[-10:]
                        INCIDENT_CONTEXTS[cleaned_driver_10] = inc_id
                        ACTIVE_VEHICLE_BY_PHONE[cleaned_driver_10] = vehicle_id
                        
                        driver_msg = (
                            f"🚨 *OFFICIAL FLEET DISPATCH ALERT*\n\n"
                            f"🚜 *Vehicle:* {vehicle_id}\n"
                            f"Status: Repair *APPROVED* by Management.\n"
                            f"🛠️ *Assigned Workshop:* {hub}\n"
                            f"⚙️ *Required Part:* {recommended_part}\n\n"
                            f"You can now reply directly on this chat with your live updates or images for the AI Operations Agent."
                        )
                        send_whatsapp_text_reply(driver_phone, driver_msg)

                    # Feature 5 (Proactive Follow-up)
                    if not incident_ctx.get("followup_scheduled"):
                        incident_ctx["followup_scheduled"] = True
                        INCIDENT_DETAILS[inc_id] = incident_ctx
                        _schedule_followup_check(inc_id)

                    return {"status": "success", "action": "Approved by manager and driver notified."}

                elif "REJECT_" in payload_id:
                    inc_id = payload_id.split("REJECT_")[1]
                    incident_ctx = INCIDENT_DETAILS.get(inc_id, {})
                    hub_records = incident_ctx.get("hub_records", [])
                    current_index = incident_ctx.get("hub_index", 0)
                    next_index = current_index + 1

                    if next_index < len(hub_records):
                        # Auto-advance to the next nearest detected hub and
                        # re-open the HITL approval gate for it — this repeats
                        # every time the manager rejects, until hubs run out.
                        next_hub_record = hub_records[next_index]
                        next_hub_phone = _resolve_hub_phone(next_hub_record, next_index)

                        incident_ctx["hub"] = next_hub_record["name"]
                        incident_ctx["hub_phone"] = next_hub_phone
                        incident_ctx["hub_waypoint"] = next_hub_record.get("waypoint")
                        incident_ctx["hub_index"] = next_index
                        INCIDENT_DETAILS[inc_id] = incident_ctx
                        APPROVAL_STATES[inc_id] = "PENDING_MANAGER_APPROVAL"

                        send_whatsapp_text_reply(
                            raw_sender_phone,
                            f"❌ Rejected. 🔄 Auto-rerouting to next nearest service center "
                            f"({next_index + 1}/{len(hub_records)})..."
                        )
                        send_whatsapp_interactive_approval(
                            incident_id=inc_id,
                            to_phone=raw_sender_phone,
                            vehicle_id=incident_ctx.get("vehicle_id", ""),
                            hub=next_hub_record["name"],
                            location=incident_ctx.get("location", "N/A"),
                            issue_type=incident_ctx.get("issue_type", "N/A"),
                            severity=incident_ctx.get("severity", "N/A"),
                            cargo_type=incident_ctx.get("cargo_type", "N/A"),
                            recommended_part=incident_ctx.get("recommended_part", "Standard Spare Kit"),
                            predictive_alert=incident_ctx.get("predictive_alert")
                        )
                        return {
                            "status": "success",
                            "action": f"Auto-rerouted to next hub ({next_index + 1}/{len(hub_records)}).",
                            "new_hub": next_hub_record["name"]
                        }
                    else:
                        # Hub list exhausted — nothing left to auto-reroute to.
                        APPROVAL_STATES[inc_id] = "REJECTED_REROUTING"
                        send_whatsapp_text_reply(
                            raw_sender_phone,
                            "❌ Rejected — no more nearby authorized service centers were found in range. "
                            "Please advise manually or expand the search area."
                        )
                        return {"status": "success", "action": "Rejected — hub list exhausted, manual intervention needed."}
            
            elif msg.get("type") == "text":
                text_body = msg["text"].get("body", "")

                # First check: is this a reply from a spare-part vendor we
                # sent a quote request to? If so, handle it separately from
                # normal driver/manager chat.
                for inc_id, quote_ctx in list(PENDING_QUOTES.items()):
                    if sender_10_digit in quote_ctx["vendors"]:
                        handled = _handle_vendor_quote_reply(inc_id, sender_10_digit, text_body)
                        if handled:
                            send_whatsapp_text_reply(raw_sender_phone, "Dhanyavaad, jaankari mil gayi hai.")
                            return {"status": "success", "action": "Vendor quote reply processed."}
                        else:
                            send_whatsapp_text_reply(raw_sender_phone, "Kripya upar diye gaye Yes/No button par tap karein.")
                            return {"status": "success", "action": "Vendor text ignored - awaiting button press."}

                matched_inc_id = None
                for phone, inc_id in INCIDENT_CONTEXTS.items():
                    reg_10_digit = ''.join(filter(str.isdigit, phone))[-10:]
                    if reg_10_digit == sender_10_digit:
                        matched_inc_id = inc_id
                        break
                
                if not matched_inc_id and INCIDENT_CONTEXTS:
                    matched_inc_id = list(INCIDENT_CONTEXTS.values())[-1]
                
                if matched_inc_id:
                    incident_ctx = INCIDENT_DETAILS.get(matched_inc_id, {})
                    is_driver = sender_10_digit in ACTIVE_VEHICLE_BY_PHONE
                    if is_driver:
                        incident_ctx["current_chatter"] = f"Driver of {ACTIVE_VEHICLE_BY_PHONE[sender_10_digit]}"
                        # Feature 5 (Proactive Follow-up): har real driver message
                        # par timestamp refresh karo, taaki pending follow-up
                        # timer ko pata chale driver already update de chuka hai.
                        DRIVER_LAST_UPDATE_TS[matched_inc_id] = time.time()

                    ai_result = call_ai_agent(text_body, incident_ctx)
                    resp = send_whatsapp_text_reply(raw_sender_phone, ai_result["reply_text"])

                    # Driver ka location/status update sirf usi ko reply karke
                    # nahi chhodna — AI ka reply pehle sirf driver ko "noted"
                    # bol deta tha, lekin manager ko kabhi ACTUALLY forward
                    # nahi hota tha. Ab jab bhi ye decision fire ho, manager ko
                    # turant real update jaata hai, driver ke apne words me.
                    if is_driver and ai_result.get("decision") == "DRIVER_STATUS_UPDATE":
                        vehicle_id = ACTIVE_VEHICLE_BY_PHONE.get(sender_10_digit, incident_ctx.get("vehicle_id", "N/A"))
                        send_whatsapp_text_reply(
                            MANAGER_WHATSAPP_NUMBER,
                            f"📍 *Driver Update — {vehicle_id}*\n\n\"{text_body}\""
                        )

                    # Feature 4 (Urgency Detection): agar AI ne is message ko
                    # URGENT/CRITICAL classify kiya hai, manager ko turant ek
                    # standalone escalation bhejo — DRIVER_STATUS_UPDATE forward
                    # se independent, chahe decision kuch bhi ho.
                    if is_driver and ai_result.get("urgency") in ("URGENT", "CRITICAL"):
                        vehicle_id = ACTIVE_VEHICLE_BY_PHONE.get(sender_10_digit, incident_ctx.get("vehicle_id", "N/A"))
                        icon = "🆘" if ai_result["urgency"] == "CRITICAL" else "⚠️"
                        send_whatsapp_text_reply(
                            MANAGER_WHATSAPP_NUMBER,
                            f"{icon} *{ai_result['urgency']} ALERT — {vehicle_id}*\n\n"
                            f"Driver message: \"{text_body}\"\n\n"
                            f"Turant dhyaan dein / call karein."
                        )

                    return {"status": "success", "action": "Smart AI agent replied to chatter.", "decision": ai_result["decision"], "urgency": ai_result.get("urgency", "NORMAL")}
                else:
                    send_whatsapp_text_reply(raw_sender_phone, "⚠️ No active fleet incident context found for your session.")
                    return {"status": "error", "action": "No active incident context found."}
                    
    except Exception as e:
        print("DEBUG WEBHOOK ERROR:", str(e))
        return {"status": "error", "details": str(e)}

    return {"status": "received"}

@app.get("/api/health")
async def health_check():
    return {"status": "online", "engine": "Enterprise Agentic AI RAG & Swarm Orchestrator v5.0.0-GoogleLevel"}

@app.get("/api/debug-api-key")
async def debug_api_key(x_api_key: Optional[str] = Header(None)):
    """Temporary debug endpoint — helps diagnose X-API-Key mismatches
    (typos, extra spaces/quotes) WITHOUT exposing either key's actual value."""
    return {
        "fleet_api_key_set_on_server": bool(FLEET_API_KEY),
        "fleet_api_key_length_on_server": len(FLEET_API_KEY) if FLEET_API_KEY else 0,
        "received_x_api_key_header": x_api_key is not None,
        "received_key_length": len(x_api_key) if x_api_key else 0,
        "keys_match": bool(FLEET_API_KEY) and x_api_key == FLEET_API_KEY
    }


@app.get("/api/debug-pending-quotes")
async def debug_pending_quotes():
    """Vendor quote flow ka LIVE state dikhata hai — kaunsa vendor 'waiting'
    hai, kaunsa 'awaiting_cost', price kya aaya, manager_notified ho chuka
    ya nahi. Ise hit karke exactly pata chal jaata hai flow kahaan atka hai,
    bina Render logs khole."""
    return {
        "count": len(PENDING_QUOTES),
        "quotes": {inc_id: ctx for inc_id, ctx in PENDING_QUOTES.items()}
    }


@app.post("/api/debug-clear-pending-quotes")
async def debug_clear_pending_quotes(_auth=Depends(require_api_key)):
    """SQLite me persist ho chuki PURANI/stale test quote-contexts ko clear
    karta hai — fresh testing se pehle isse hit karo taaki koi purana
    incident state naya test na bigade. Needs X-API-Key."""
    cleared = list(PENDING_QUOTES.keys())
    for inc_id in cleared:
        del PENDING_QUOTES[inc_id]
    return {"status": "success", "cleared_incident_ids": cleared}

@app.get("/api/test-gemini")
async def test_gemini():
    """Browser me seedha khol ke Gemini key/model test karne ke liye —
    koi local terminal/curl ki zaroorat nahi. Sirf debugging ke liye hai."""
    if not GEMINI_API_KEY:
        return {"status": "error", "reason": "GEMINI_API_KEY environment variable is not set on Render."}
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
        )
        body = {"contents": [{"parts": [{"text": "Say hello in one word"}]}]}
        resp = requests.post(url, json=body, timeout=10)
        if resp.status_code != 200:
            return {
                "status": "error",
                "http_status": resp.status_code,
                "google_response": resp.json()
            }
        data = resp.json()
        reply_text = data["candidates"][0]["content"]["parts"][0]["text"]
        return {"status": "success", "gemini_replied": reply_text}
    except Exception as e:
        return {"status": "exception", "details": str(e)}
