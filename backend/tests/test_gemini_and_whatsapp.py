"""Gemini and WhatsApp clients against fake HTTP — no real calls, no real keys."""
import json

import pytest
import requests

from conftest import DRIVER, FakeResponse, MANAGER
from fleet import gemini, knowledge, whatsapp
from fleet.config import cfg


def _gemini_reply(text, status=200):
    return FakeResponse(status, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


@pytest.fixture
def gem(monkeypatch):
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "gem-key")
    calls = []
    queue = []

    def post(url, json=None, headers=None, timeout=None, **_):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(requests, "post", post)
    return type("G", (), {"calls": calls, "queue": queue})


# ---------- parsing / fences ----------
def test_parse_json_reply_handles_fences():
    assert gemini._parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert gemini._parse_json_reply('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        gemini._parse_json_reply("sorry, no json")


def test_key_travels_in_a_header_never_in_the_url(gem):
    gem.queue.append(_gemini_reply('{"issue_type": "x"}'))
    gemini.transcribe_voice_note(b"audio")
    call = gem.calls[0]
    assert call["headers"]["x-goog-api-key"] == "gem-key" and "gem-key" not in call["url"]


# ---------- voice ----------
def test_transcribe_without_key_returns_none():
    assert gemini.transcribe_voice_note(b"a") is None


def test_transcribe_success_and_defaults(gem):
    gem.queue.append(_gemini_reply('```json\n{"vehicle_id": "BR1", "location": "Gaya", "issue_type": "no start"}\n```'))
    assert gemini.transcribe_voice_note(b"a") == {"vehicle_id": "BR1", "location": "Gaya", "issue_type": "no start"}
    gem.queue.append(_gemini_reply('{"issue_type": "no start"}'))
    out = gemini.transcribe_voice_note(b"a")
    assert out["vehicle_id"] == "UNKNOWN" and out["location"] == "UNKNOWN"


@pytest.mark.parametrize("reply", ['{"vehicle_id": "BR1"}', "garbage", ""])
def test_transcribe_bad_replies_return_none(gem, reply):
    gem.queue.append(_gemini_reply(reply))
    assert gemini.transcribe_voice_note(b"a") is None


def test_transcribe_http_failure_returns_none(gem):
    gem.queue.append(requests.ConnectionError("down"))
    assert gemini.transcribe_voice_note(b"a") is None
    gem.queue.append(_gemini_reply("{}", status=500))
    assert gemini.transcribe_voice_note(b"a") is None


# ---------- photo ----------
def test_photo_diagnosis_fills_defaults_and_clamps_severity(gem):
    gem.queue.append(_gemini_reply('{"driver_message": "ok", "severity": "APOCALYPSE"}'))
    out = gemini.diagnose_incident_photo(b"img", "image/jpeg", {})
    assert out["severity"] == "MODERATE" and out["can_self_fix"] is False and out["diy_steps"] == []


def test_photo_diagnosis_failures_return_none(gem):
    assert gem.calls == []
    assert gemini.diagnose_incident_photo(b"i", "image/jpeg", {}) is None   # no key set -> no call
    gem.queue.append(_gemini_reply('{"visible_damage": "x"}'))        # no driver_message
    assert gemini.diagnose_incident_photo(b"i", "image/jpeg", {}) is None


# ---------- chat agent ----------
def test_agent_without_key_uses_keyword_fallback():
    assert gemini.call_ai_agent("approve", {})["decision"] == "APPROVED_AND_DISPATCHED"


def test_agent_parses_reply_and_defaults_urgency(gem):
    gem.queue.append(_gemini_reply('{"decision": "INFO_REQUEST_ANSWERED", "reply_text": "Hub: X"}'))
    out = gemini.call_ai_agent("phone?", {"hub": "X"})
    assert out["reply_text"] == "Hub: X" and out["urgency"] == "NORMAL"


def test_agent_clamps_unknown_urgency(gem):
    gem.queue.append(_gemini_reply('{"decision": "D", "reply_text": "r", "urgency": "PANIC"}'))
    assert gemini.call_ai_agent("x", {})["urgency"] == "NORMAL"


def test_agent_retries_on_503_with_the_lite_model(gem):
    gem.queue.extend([_gemini_reply("", status=503), _gemini_reply('{"decision": "D", "reply_text": "r"}')])
    assert gemini.call_ai_agent("x", {})["reply_text"] == "r"
    assert "flash-lite" in gem.calls[1]["url"]


def test_agent_falls_back_to_keywords_when_gemini_is_down(gem):
    gem.queue.extend([requests.Timeout("slow"), requests.Timeout("slow")])
    assert gemini.call_ai_agent("accident ho gaya", {})["urgency"] == "CRITICAL"


def test_agent_survives_garbage_replies(gem):
    gem.queue.extend([_gemini_reply("not json"), _gemini_reply('{"decision": "", "reply_text": ""}')])
    assert gemini.call_ai_agent("approve", {})["decision"] == "APPROVED_AND_DISPATCHED"


# ---------- AI diagnosis (CRAG fallback) ----------
def test_corrective_rag_uses_ai_guess_and_marks_it(gem, monkeypatch):
    monkeypatch.setattr(knowledge, "generate_ai_diagnosis",
                        lambda q: {"recommended_part": "Widget", "diagnostic_summary": "guess"})
    out = knowledge.CorrectiveRAGValidator.evaluate_and_correct(
        "weird noise", {"recommended_part": "Gadget (Part #ZZ-999)", "_grounding_text": "unrelated"})
    assert out["hallucination_grade"] == "AI_GENERATED_FALLBACK" and out["recommended_part"] == "Widget"


def test_corrective_rag_falls_back_to_safe_kit_without_ai(monkeypatch):
    monkeypatch.setattr(knowledge, "generate_ai_diagnosis", lambda q: None)
    out = knowledge.CorrectiveRAGValidator.evaluate_and_correct(
        "weird noise", {"recommended_part": "Gadget (Part #ZZ-999)", "_grounding_text": "unrelated"})
    assert out["hallucination_grade"] == "CORRECTED_TO_SAFE_BASELINE" and "FL-GEN-01" in out["recommended_part"]


def test_corrective_rag_passes_a_grounded_part(monkeypatch):
    out = knowledge.CorrectiveRAGValidator.evaluate_and_correct(
        "leak", {"recommended_part": "Hose (Part #H-1)", "cross_encoder_rerank_confidence": 0.9,
                 "_grounding_text": "Fix it. Recommended part: Hose (Part #H-1)"})
    assert out["hallucination_grade"] == "PASSED_VERIFIED_AUTHENTIC"


# ---------- learned documents ----------
def test_add_learned_document_dedupes_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "KNOWLEDGE_BASE_DIR", str(tmp_path / "kb"))

    class StubIndex:
        def __init__(self, documents):
            self.documents = documents
    monkeypatch.setattr(knowledge, "_hybrid_engine_cls", lambda: StubIndex)
    engine = knowledge.AdvancedHybridRAGEngine()
    doc = {"id": "AUTOLEARN-1", "manual": "m", "keywords": ["k"], "part_code": "P", "content": "c"}

    assert engine.add_learned_document(dict(doc)) is True
    assert engine.add_learned_document(dict(doc)) is False             # same id: no growth
    saved = json.loads((tmp_path / "kb" / "auto_learned.json").read_text())
    assert [d["id"] for d in saved] == ["AUTOLEARN-1"]
    assert any(d["id"] == "AUTOLEARN-1" for d in engine._engine.documents and engine.knowledge_base)


# ---------- WhatsApp client ----------
def test_senders_simulate_without_a_token():
    assert whatsapp.send_whatsapp_text_reply("+15555550100", "hi") == {"status": "simulated_reply", "text": "hi"}
    assert whatsapp.send_whatsapp_interactive_approval(
        "INC-1", MANAGER, "V", "H", "L", "I", "S", "C", "P")["status"] == "simulated_interactive_dispatched"
    assert whatsapp.download_media("m1") is None


def test_placeholder_token_counts_as_simulated(monkeypatch):
    monkeypatch.setattr(cfg, "WHATSAPP_TOKEN", "YOUR_TOKEN")
    assert whatsapp.send_whatsapp_text_reply("1", "hi")["status"] == "simulated_reply"


def test_text_reply_goes_to_digits_only_with_bearer_token(wa):
    whatsapp.send_whatsapp_text_reply("+1 (555) 555-0101", "hello")
    url, body = wa.calls[0]
    assert url.endswith("/1234567890/messages")
    assert body["to"] == "15555550101" and body["text"] == {"body": "hello"}


def test_approval_card_has_approve_and_reject_buttons(wa):
    res = whatsapp.send_whatsapp_interactive_approval("INC-9", MANAGER, "V1", "Hub", "Loc", "Issue", "HIGH", "Grain", "Part",
                                                      predictive_alert="REPEAT!")
    assert res["status"] == "dispatched_via_meta_api"
    assert wa.button_ids(MANAGER) == ["APPROVE_INC-9", "REJECT_INC-9"]
    assert "REPEAT!" in json.dumps(wa.calls[0][1])


def test_approval_card_reports_meta_errors_for_the_dashboard(wa):
    wa.status_code = 400
    res = whatsapp.send_whatsapp_interactive_approval("INC-9", MANAGER, "V1", "H", "L", "I", "S", "C", "P")
    assert res["status"] == "meta_api_error" and res["status_code"] == 400 and "error_details" in res


def test_approval_card_survives_network_failure_and_non_json(wa, monkeypatch):
    wa.raise_exc = requests.ConnectionError("down")
    res = whatsapp.send_whatsapp_interactive_approval("INC-9", MANAGER, "V1", "H", "L", "I", "S", "C", "P")
    assert res == {"status": "meta_api_error", "status_code": None, "error_details": "ConnectionError"}


def test_button_senders_signal_failure_so_callers_can_fall_back(wa):
    wa.status_code = 500
    assert whatsapp.send_driver_accept_repair_button(DRIVER, "INC-1", "body")["delivery_failed"] is True
    wa.status_code, wa.raise_exc = 200, requests.Timeout("slow")
    assert whatsapp.send_driver_accept_repair_button(DRIVER, "INC-1", "body")["status"] == "error"


def test_vendor_choice_titles_fit_whatsapps_20_char_limit(wa):
    quoted = [{"hub_name": "A" * 80, "price": 1234567.0, "vendor_key": f"k{i}"} for i in range(5)]
    whatsapp.send_vendor_choice_buttons("INC-1", quoted, "Part")
    buttons = wa.interactive(MANAGER)[0]["action"]["buttons"]
    assert len(buttons) == 3 and all(len(b["reply"]["title"]) <= 20 for b in buttons)


def test_download_media_two_step_and_failure(wa, monkeypatch):
    seen = []

    def get(url, headers=None, timeout=None, **_):
        seen.append((url, timeout))
        if "cdn" not in url:
            return FakeResponse(200, {"url": "https://cdn.example/file"})
        file_resp = FakeResponse(200, {})
        file_resp.content = b"BYTES"
        return file_resp
    monkeypatch.setattr(requests, "get", get)
    assert whatsapp.download_media("m1") == b"BYTES"
    assert [t for _, t in seen] == [10, 15]

    def boom(*a, **k):
        raise requests.ConnectionError("x")
    monkeypatch.setattr(requests, "get", boom)
    assert whatsapp.download_media("m1") is None


def test_forward_image_is_skipped_when_simulated():
    assert whatsapp.forward_image(MANAGER, "m1")["status"] == "simulated_or_skipped"
