"""The FastAPI app: routing and HTTP concerns only. Every rule lives in the modules it calls."""
from typing import Any, Dict, Optional

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool

from fleet import auth, state
from fleet.config import cfg
from fleet.knowledge import get_engine
from fleet.models import IncidentInput, TriageResponse
from fleet.orchestrator import EnterpriseAgenticRAGOrchestrator
from fleet.webhook import handle_webhook_body
from fleet.whatsapp import send_whatsapp_interactive_approval

app = FastAPI(title="Autonomous Enterprise Fleet Agentic AI & RAG Engine", version="5.0.0-GoogleLevel")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)

for _line in cfg.startup_warnings():
    print(_line)
get_engine()  # build the search index at startup so the first request is not slow


def _guard(check, *args) -> None:
    """Run a framework-free auth check and translate its AuthError into an HTTP error."""
    try:
        check(*args)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    _guard(auth.check_api_key, x_api_key)


def rate_limit(request: Request) -> None:
    _guard(auth.check_rate_limit, request.client.host if request.client else "unknown")


def debug_only() -> None:
    _guard(auth.check_debug_enabled)


# Handlers that do blocking work (Gemini, Maps, WhatsApp, SQLite) are plain `def`, so
# FastAPI runs them on its thread pool instead of freezing the event loop.
@app.post("/api/triage", response_model=TriageResponse)
def trigger_triage(incident: IncidentInput, _auth=Depends(require_api_key), _rl=Depends(rate_limit)):
    return EnterpriseAgenticRAGOrchestrator(incident).run_swarm()


@app.get("/api/approval-status/{incident_id}")
def get_approval_status(incident_id: str):
    # Deliberately unauthenticated: the dashboard polls it, and incident ids are random.
    return {
        "incident_id": incident_id,
        "approval_status": state.APPROVAL_STATES.get(incident_id, "PENDING_MANAGER_APPROVAL"),
    }


@app.post("/api/send-whatsapp-interactive")
def send_whatsapp_interactive(payload: Dict[str, Any], _auth=Depends(require_api_key), _rl=Depends(rate_limit)):
    incident_id = payload.get("incident_id")
    if not incident_id or not payload.get("phone"):
        raise HTTPException(status_code=422, detail="incident_id and phone are required.")
    # Feature 6: show the predictive-maintenance alert run_swarm stored for this incident.
    predictive_alert = state.INCIDENT_DETAILS.get(incident_id, {}).get("predictive_alert") if incident_id else None
    return send_whatsapp_interactive_approval(
        incident_id=incident_id,
        to_phone=payload.get("phone", ""),
        vehicle_id=payload.get("vehicle_id"),
        hub=payload.get("hub"),
        location=payload.get("location", "N/A"),
        issue_type=payload.get("issue_type", "N/A"),
        severity=payload.get("severity", "N/A"),
        cargo_type=payload.get("cargo_type", "N/A"),
        recommended_part=payload.get("recommended_part", "Standard Spare Kit"),
        predictive_alert=predictive_alert,
    )


@app.get("/api/whatsapp-webhook")
def verify_whatsapp_webhook(request: Request):
    q = request.query_params
    try:
        return auth.verify_handshake(q.get("hub.mode"), q.get("hub.verify_token"), q.get("hub.challenge"))
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/api/whatsapp-webhook")
async def whatsapp_webhook(request: Request):
    # Needs the raw bytes (the signature covers them), and the check runs outside the
    # handler's error handling so a bad signature is a real 403, not a swallowed exception.
    body = await request.body()
    _guard(auth.check_webhook_signature, body, request.headers.get("x-hub-signature-256"))
    return await run_in_threadpool(handle_webhook_body, body)


@app.get("/api/health")
def health_check():
    return {"status": "online", "engine": "Enterprise Agentic AI RAG & Swarm Orchestrator v5.0.0-GoogleLevel"}


# ---- Diagnostics: do not exist unless ENABLE_DEBUG_ENDPOINTS=true, and need the API key ----
@app.get("/api/debug-pending-quotes")
def debug_pending_quotes(_dbg=Depends(debug_only), _auth=Depends(require_api_key)):
    """Live state of the vendor-quote flow (who is waiting / awaiting_cost / quoted)."""
    return {"count": len(state.PENDING_QUOTES), "quotes": dict(state.PENDING_QUOTES.items())}


@app.post("/api/debug-clear-pending-quotes")
def debug_clear_pending_quotes(_dbg=Depends(debug_only), _auth=Depends(require_api_key)):
    """Clear stale quote contexts persisted in SQLite before a fresh test run."""
    with state.STATE_LOCK:
        cleared = list(state.PENDING_QUOTES.keys())
        for inc_id in cleared:
            del state.PENDING_QUOTES[inc_id]
    return {"status": "success", "cleared_incident_ids": cleared}


@app.get("/api/test-gemini")
def test_gemini(_dbg=Depends(debug_only), _auth=Depends(require_api_key), _rl=Depends(rate_limit)):
    """Check that the Gemini key/model work."""
    if not cfg.GEMINI_API_KEY:
        return {"status": "error", "reason": "GEMINI_API_KEY environment variable is not set."}
    try:
        # Key goes in a header, not the URL, so it can never leak via an exception message.
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
        body = {"contents": [{"parts": [{"text": "Say hello in one word"}]}]}
        resp = requests.post(url, json=body, headers={"x-goog-api-key": cfg.GEMINI_API_KEY}, timeout=10)
        if resp.status_code != 200:
            return {"status": "error", "http_status": resp.status_code}
        return {"status": "success", "gemini_replied": resp.json()["candidates"][0]["content"]["parts"][0]["text"]}
    except Exception as e:  # noqa: BLE001
        return {"status": "exception", "details": type(e).__name__}
