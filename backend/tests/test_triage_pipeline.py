"""The /api/triage pipeline (orchestrator), run with a fake retrieval engine and no network."""
import pytest

from conftest import DRIVER, MANAGER, VENDOR_A, VENDOR_B
from fleet import knowledge, state
from fleet.config import cfg
from fleet.models import IncidentInput
from fleet.orchestrator import EnterpriseAgenticRAGOrchestrator


class FakeEngine:
    """Returns a manual-grounded hit, like the real engine does for a known issue."""

    def hybrid_retrieve_and_rerank(self, query):
        return {
            "recommended_part": "Radiator Hose Kit (Part #T-1)",
            "referenced_manual": "Manual",
            "diagnostic_summary": "Hose cracked",
            "cross_encoder_rerank_confidence": 0.95,
            "vector_match_score": 0.9,
            "bm25_keyword_score": 0.8,
            "_grounding_text": "Replace hose. Recommended part: Radiator Hose Kit (Part #T-1)",
        }


@pytest.fixture
def engine():
    knowledge.set_engine(FakeEngine())


def _incident(**kw):
    base = dict(vehicle_id="TEST01", location="Patna", issue_type="radiator leak",
                severity="HIGH", cargo_type="Grain")
    return IncidentInput(**{**base, **kw})


def test_triage_registers_incident_for_manager_approval(engine, wa):
    out = EnterpriseAgenticRAGOrchestrator(_incident()).run_swarm()

    assert out.status == "AWAITING_HITL_APPROVAL" and out.approval_status == "PENDING_MANAGER_APPROVAL"
    assert out.assigned_whatsapp_number == MANAGER
    assert out.final_resolution["rag_diagnostic_part"].startswith("Radiator Hose Kit")
    ctx = state.INCIDENT_DETAILS[out.incident_id]
    assert ctx["vehicle_id"] == "TEST01" and ctx["hub_records"] and ctx["followup_scheduled"] is False
    assert state.INCIDENT_CONTEXTS[MANAGER] == out.incident_id
    assert len(out.traces) == 4


def test_triage_flags_repeat_issues_on_the_same_vehicle(engine, wa):
    alerts = [EnterpriseAgenticRAGOrchestrator(_incident()).run_swarm().final_resolution["predictive_maintenance_alert"]
              for _ in range(3)]
    assert alerts[0] is None and alerts[1] is None and "Predictive Maintenance" in alerts[2]


def test_triage_opens_a_vendor_quote_round(engine, wa, monkeypatch):
    monkeypatch.setattr(cfg, "TEST_VENDOR_NUMBERS",
                        [{"name": "A", "phone": VENDOR_A}, {"name": "B", "phone": VENDOR_B}])
    out = EnterpriseAgenticRAGOrchestrator(_incident()).run_swarm()
    assert set(state.PENDING_QUOTES[out.incident_id]["vendors"]) == {"5555550102", "5555550103"}
    assert wa.button_ids(VENDOR_A) and wa.button_ids(VENDOR_B)


def test_vision_step_never_fetches_internal_urls(engine, wa, monkeypatch):
    from fleet import orchestrator
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "k")
    fetched = []

    def spy(url, **kw):
        fetched.append(url)
        raise ValueError("refusing to fetch a non-public or non-http(s) URL")
    monkeypatch.setattr(orchestrator, "fetch_public_image", spy)
    out = EnterpriseAgenticRAGOrchestrator(_incident(image_url="http://169.254.169.254/latest/meta-data")).run_swarm()
    assert fetched == ["http://169.254.169.254/latest/meta-data"]
    assert out.status == "AWAITING_HITL_APPROVAL"       # the refusal degrades to the text-only path


def test_incident_input_rejects_oversized_fields():
    with pytest.raises(ValueError):
        _incident(location="x" * 301)
    with pytest.raises(ValueError):
        _incident(issue_type="x" * 501)
    with pytest.raises(ValueError):
        _incident(image_url="http://x/" + "a" * 2000)
    assert _incident(location="x" * 300).location == "x" * 300


def test_driver_without_mapping_still_triages(engine, wa, monkeypatch):
    monkeypatch.setattr(cfg, "DRIVER_WHATSAPP_MAPPING", {})
    assert EnterpriseAgenticRAGOrchestrator(_incident()).run_swarm().status == "AWAITING_HITL_APPROVAL"
    assert DRIVER not in " ".join(wa.all_text().split())
