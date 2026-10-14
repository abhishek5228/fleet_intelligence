"""Autonomous Fleet Intelligence Engine — application package.

Layout (each module has one job):

    config        environment-driven settings, secure by default
    store/state   thread-safe SQLite-backed state shared by every flow
    auth          API-key, webhook-signature and rate-limit checks (no web framework needed)
    whatsapp      outbound WhatsApp Business Cloud API calls
    gemini        Gemini calls (voice, photo, diagnosis, chat) + keyword fallback
    knowledge     repair-manual knowledge base, hybrid retrieval, corrective RAG
    orchestrator  the triage pipeline behind POST /api/triage
    quotes        spare-part price comparison and one-round negotiation
    followup      proactive check-ins with silent drivers
    insights      repeat-issue detection and self-learning knowledge-base docs
    webhook/      inbound WhatsApp messages: one small handler per message type
    api           FastAPI app: routes and dependencies only
"""
