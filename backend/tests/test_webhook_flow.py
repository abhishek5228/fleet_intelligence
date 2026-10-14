"""End-to-end tests of the WhatsApp webhook flow, driven through `handle_webhook_body`.

Outbound WhatsApp calls are captured by the `wa` recorder; nothing touches the network.
"""
import json

import pytest

from conftest import DRIVER, MANAGER, STRANGER, VEHICLE, VENDOR_A, VENDOR_B, digits_of
from fleet import state
from fleet.config import cfg
from fleet.phones import last10


# ------------------------------------------------------------------ approve
def test_manager_approve_dispatches_driver_and_arms_followup_once(wa, incident, press, timers):
    res = press(MANAGER, f"APPROVE_{incident}")

    assert res["status"] == "success"
    assert state.APPROVAL_STATES[incident] == "APPROVED_AND_DISPATCHED"
    assert any("OFFICIAL FLEET DISPATCH" in t for t in wa.texts(DRIVER))
    assert state.INCIDENT_CONTEXTS[last10(DRIVER)] == incident
    assert state.ACTIVE_VEHICLE_BY_PHONE[last10(DRIVER)] == VEHICLE
    assert [t.interval for t in timers.instances] == [7200] and timers.instances[0].daemon

    # A second tap neither re-notifies the driver nor arms another timer.
    wa.clear()
    again = press(MANAGER, f"APPROVE_{incident}")
    assert again["action"] == "Already approved."
    assert wa.texts(DRIVER) == [] and len(timers.instances) == 1


@pytest.mark.parametrize("who", [DRIVER, STRANGER, VENDOR_A])
@pytest.mark.parametrize("button", ["APPROVE_{i}", "REJECT_{i}", "PICKVENDOR_{i}_5555550102"])
def test_manager_only_buttons_refuse_everyone_else(wa, incident, press, who, button):
    res = press(who, button.format(i=incident))

    assert res["status"] == "forbidden"
    assert state.APPROVAL_STATES[incident] == "PENDING_MANAGER_APPROVAL"
    assert wa.texts(DRIVER) == [] or who == DRIVER
    assert any("authorized nahi" in t for t in wa.texts(who))


def test_approve_unknown_incident_changes_nothing(wa, press):
    res = press(MANAGER, "APPROVE_INC-DOESNOTEXIST")
    assert res["status"] == "error"
    assert "INC-DOESNOTEXIST" not in state.APPROVAL_STATES


class _Engine:
    def __init__(self, learned):
        self.learned, self.docs = learned, []

    def add_learned_document(self, doc):
        self.docs.append(doc)
        return self.learned


@pytest.mark.parametrize("learned,expect_msg", [(True, True), (False, False)])
def test_approving_an_ai_guess_teaches_the_knowledge_base(wa, incident, press, learned, expect_msg):
    from fleet import knowledge
    engine = _Engine(learned)
    knowledge.set_engine(engine)
    ctx = state.INCIDENT_DETAILS[incident]
    ctx["hallucination_grade"] = "AI_GENERATED_FALLBACK"
    state.INCIDENT_DETAILS[incident] = ctx

    press(MANAGER, f"APPROVE_{incident}")

    assert len(engine.docs) == 1 and engine.docs[0]["id"].startswith("AUTOLEARN-")
    said = any("Knowledge base updated" in t for t in wa.texts(MANAGER))
    assert said is expect_msg


def test_learning_failure_does_not_undo_the_approval(wa, incident, press):
    from fleet import knowledge

    class Boom:
        def add_learned_document(self, doc):
            raise RuntimeError("index broken")
    knowledge.set_engine(Boom())
    ctx = state.INCIDENT_DETAILS[incident]
    ctx["hallucination_grade"] = "AI_GENERATED_FALLBACK"
    state.INCIDENT_DETAILS[incident] = ctx

    assert press(MANAGER, f"APPROVE_{incident}")["status"] == "success"
    assert state.APPROVAL_STATES[incident] == "APPROVED_AND_DISPATCHED"


def test_grounded_diagnosis_is_not_learned(wa, incident, press):
    from fleet import knowledge
    engine = _Engine(True)
    knowledge.set_engine(engine)
    press(MANAGER, f"APPROVE_{incident}")
    assert engine.docs == []


# ------------------------------------------------------------------ reject / reroute
def test_reject_reroutes_to_next_hub_then_runs_out(wa, incident, press):
    first = press(MANAGER, f"REJECT_{incident}")
    assert first["new_hub"] == "Hub Two"
    assert state.INCIDENT_DETAILS[incident]["hub"] == "Hub Two"
    assert state.APPROVAL_STATES[incident] == "PENDING_MANAGER_APPROVAL"
    assert f"APPROVE_{incident}" in wa.button_ids(MANAGER)   # approval gate re-opened

    second = press(MANAGER, f"REJECT_{incident}")
    assert "exhausted" in second["action"]
    assert state.APPROVAL_STATES[incident] == "REJECTED_REROUTING"


def test_cannot_reject_an_approved_repair(wa, incident, press):
    press(MANAGER, f"APPROVE_{incident}")
    res = press(MANAGER, f"REJECT_{incident}")
    assert res["status"] == "error"
    assert state.APPROVAL_STATES[incident] == "APPROVED_AND_DISPATCHED"


# ------------------------------------------------------------------ vendor quote flow
@pytest.fixture
def quotes(wa, incident, monkeypatch):
    """Open a quote round with two test vendors, like the triage pipeline does."""
    from fleet.quotes import request_spare_part_quotes
    monkeypatch.setattr(cfg, "TEST_VENDOR_NUMBERS",
                        [{"name": "Vendor A", "phone": VENDOR_A}, {"name": "Vendor B", "phone": VENDOR_B}])
    request_spare_part_quotes(incident, [], "Radiator hose")
    wa.clear()
    return incident


def test_quote_round_sends_yes_no_buttons_and_arms_a_40s_timer(wa, incident, monkeypatch, timers):
    from fleet.quotes import request_spare_part_quotes
    monkeypatch.setattr(cfg, "TEST_VENDOR_NUMBERS", [{"name": "Vendor A", "phone": VENDOR_A}])
    request_spare_part_quotes(incident, [], "Radiator hose")

    assert wa.button_ids(VENDOR_A) == [f"VENDORAVAIL_YES_{incident}_{last10(VENDOR_A)}",
                                       f"VENDORAVAIL_NO_{incident}_{last10(VENDOR_A)}"]
    assert [(t.interval, t.daemon) for t in timers.instances] == [(40.0, True)]


def test_full_vendor_flow_from_quote_to_driver_accepting(wa, quotes, press, say, timers):
    inc, a, b = quotes, last10(VENDOR_A), last10(VENDOR_B)

    assert press(VENDOR_A, f"VENDORAVAIL_YES_{inc}_{a}")["action"].endswith("awaiting cost.")
    assert say(VENDOR_A, "Rs 1,200")["action"] == "Vendor quote reply processed."
    assert state.PENDING_QUOTES[inc]["vendors"][a]["price"] == 1200.0

    press(VENDOR_B, f"VENDORAVAIL_NO_{inc}_{b}")          # last vendor answers -> manager is told
    assert wa.button_ids(MANAGER) == [f"PICKVENDOR_{inc}_{a}"]

    res = press(MANAGER, f"PICKVENDOR_{inc}_{a}")
    assert res["status"] == "success"
    assert state.APPROVAL_STATES[inc] == "APPROVED_AND_DISPATCHED"
    assert inc not in state.PENDING_QUOTES
    assert wa.button_ids(DRIVER) == [f"ACCEPTREPAIR_{inc}"]

    # The driver is not chat-enabled until they tap Accept Repair.
    assert last10(DRIVER) not in state.INCIDENT_CONTEXTS
    assert press(DRIVER, f"ACCEPTREPAIR_{inc}")["status"] == "success"
    assert state.INCIDENT_CONTEXTS[last10(DRIVER)] == inc
    assert any("Vendor A" in b["text"]["body"] for b in wa.to(MANAGER) if b["type"] == "text")
    assert any(t.interval == 7200 for t in timers.instances)


def test_vendor_is_asked_to_negotiate_without_seeing_rival_price(wa, quotes, press, say):
    inc, a, b = quotes, last10(VENDOR_A), last10(VENDOR_B)
    for who, key in ((VENDOR_A, a), (VENDOR_B, b)):
        press(who, f"VENDORAVAIL_YES_{inc}_{key}")
    say(VENDOR_A, "1000")
    wa.clear()
    say(VENDOR_B, "1500")

    ask = " ".join(wa.texts(VENDOR_B))
    assert "better rate" in ask and "1000" not in ask
    assert state.PENDING_QUOTES[inc]["vendors"][b]["status"] == "negotiating"

    say(VENDOR_B, "1100")
    assert state.PENDING_QUOTES[inc]["vendors"][b]["status"] == "quoted"
    assert state.PENDING_QUOTES[inc]["vendors"][b]["price"] == 1100.0
    assert len(wa.button_ids(MANAGER)) == 2   # both quoted -> manager gets both choices


def test_vendor_text_before_pressing_a_button_is_ignored(wa, quotes, say):
    res = say(VENDOR_A, "1200")
    assert res["action"].startswith("Vendor text ignored")
    assert state.PENDING_QUOTES[quotes]["vendors"][last10(VENDOR_A)]["price"] is None


def test_someone_else_cannot_answer_for_a_vendor(wa, quotes, press):
    res = press(STRANGER, f"VENDORAVAIL_YES_{quotes}_{last10(VENDOR_A)}")
    assert res["status"] == "forbidden"
    assert state.PENDING_QUOTES[quotes]["vendors"][last10(VENDOR_A)]["awaiting_cost"] is False


def test_expired_quote_is_reported_not_crashed(wa, incident, press):
    res = press(VENDOR_A, f"VENDORAVAIL_YES_{incident}_{last10(VENDOR_A)}")
    assert res["status"] == "error"
    assert press(MANAGER, f"PICKVENDOR_{incident}_{last10(VENDOR_A)}")["status"] == "error"


def test_timeout_sends_whatever_quotes_are_ready(wa, quotes, press, say, timers):
    inc, a = quotes, last10(VENDOR_A)
    press(VENDOR_A, f"VENDORAVAIL_YES_{inc}_{a}")
    say(VENDOR_A, "900")
    assert wa.button_ids(MANAGER) == []            # vendor B has not answered yet
    next(t for t in timers.instances if t.interval == 40.0).fire()
    assert wa.button_ids(MANAGER) == [f"PICKVENDOR_{inc}_{a}"]


def test_malformed_button_ids_are_ignored(wa, incident, press):
    for bad in ("", "APPROVE", "VENDORAVAIL_YES_", "VENDORAVAIL_YES_nounderscore", "PICKVENDOR_x", "HELLO_WORLD"):
        assert press(MANAGER, bad)["status"] in ("ignored", "error")
    assert state.APPROVAL_STATES[incident] == "PENDING_MANAGER_APPROVAL"


# ------------------------------------------------------------------ accept repair
def test_only_the_incidents_driver_can_accept(wa, incident, press):
    assert press(STRANGER, f"ACCEPTREPAIR_{incident}")["status"] == "forbidden"
    assert press(MANAGER, f"ACCEPTREPAIR_{incident}")["status"] == "forbidden"
    assert last10(STRANGER) not in state.INCIDENT_CONTEXTS
    assert last10(STRANGER) not in state.ACTIVE_VEHICLE_BY_PHONE


def test_accept_repair_arms_followup_only_once(wa, incident, press, timers):
    press(DRIVER, f"ACCEPTREPAIR_{incident}")
    press(DRIVER, f"ACCEPTREPAIR_{incident}")
    assert len([t for t in timers.instances if t.interval == 7200]) == 1


# ------------------------------------------------------------------ chat
@pytest.fixture
def active_driver(wa, incident, press):
    press(DRIVER, f"ACCEPTREPAIR_{incident}")
    wa.clear()
    return incident


def test_driver_status_update_is_forwarded_to_manager(wa, active_driver, say):
    res = say(DRIVER, "on my way to the hub")
    assert res["decision"] == "DRIVER_STATUS_UPDATE"
    assert any("Driver Update" in t and "on my way" in t for t in wa.texts(MANAGER))
    assert wa.texts(DRIVER)                                   # driver got an answer too
    assert state.DRIVER_LAST_UPDATE_TS[active_driver] > 0


def test_critical_driver_message_escalates_to_manager(wa, active_driver, say):
    say(DRIVER, "accident ho gaya")
    assert any("CRITICAL ALERT" in t for t in wa.texts(MANAGER))


def test_manager_can_chat_with_the_agent(wa, incident, say):
    res = say(MANAGER, "approve")
    assert res["status"] == "success" and res["decision"] == "APPROVED_AND_DISPATCHED"
    assert wa.texts(MANAGER)


def test_stranger_gets_no_access_to_any_incident(wa, incident, say):
    res = say(STRANGER, "what is the hub phone number?")
    assert res["status"] == "error"
    leaked = " ".join(wa.texts(STRANGER))
    assert "Hub One" not in leaked and VEHICLE not in leaked and "No active fleet incident" in leaked
    assert wa.texts(MANAGER) == []


def test_registered_driver_who_has_not_accepted_cannot_chat(wa, incident, say):
    assert say(DRIVER, "hello")["status"] == "error"


def test_ai_failure_is_contained(wa, incident, say, monkeypatch):
    from fleet.webhook import chat

    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(chat, "call_ai_agent", boom)
    res = say(MANAGER, "hello")
    assert res["status"] == "error"      # returned, not raised -> Meta still gets a 200
    assert any("dobara try" in t for t in wa.texts(MANAGER))


# ------------------------------------------------------------------ photos
def test_photo_without_gemini_is_forwarded_for_manual_review(wa, active_driver, send, monkeypatch):
    from fleet.webhook import media
    monkeypatch.setattr(media, "download_media", lambda mid: b"img")
    send(DRIVER, "image", image={"id": "m1", "mime_type": "image/jpeg"})
    assert any("forward kar diya" in t for t in wa.texts(DRIVER))
    assert any("review manually" in t for t in wa.texts(MANAGER))
    assert any(b.get("type") == "image" for b in wa.to(MANAGER))


def test_photo_diagnosis_reaches_driver_and_manager(wa, active_driver, send, monkeypatch):
    from fleet.webhook import media
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(media, "download_media", lambda mid: b"img")
    monkeypatch.setattr(media, "diagnose_incident_photo", lambda b, m, ctx: {
        "driver_message": "Hose is split", "severity": "CRITICAL", "can_self_fix": False,
        "visible_damage": "split hose", "recommended_part": "Hose", "diy_steps": []})

    res = send(DRIVER, "image", image={"id": "m1"})

    assert res["severity"] == "CRITICAL"
    assert any("Hose is split" in t for t in wa.texts(DRIVER))
    assert any("CRITICAL ALERT" in t for t in wa.texts(MANAGER))
    assert state.INCIDENT_DETAILS[active_driver]["severity"] == "CRITICAL"


def test_photo_diagnosis_failure_and_download_failure_are_reported(wa, active_driver, send, monkeypatch):
    from fleet.webhook import media
    monkeypatch.setattr(media, "download_media", lambda mid: None)
    assert send(DRIVER, "image", image={"id": "m1"})["action"] == "Image download failed."
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(media, "download_media", lambda mid: b"img")
    monkeypatch.setattr(media, "diagnose_incident_photo", lambda *a: None)
    assert send(DRIVER, "image", image={"id": "m2"})["action"] == "Photo diagnosis failed."


def test_photo_from_stranger_is_refused_without_downloading(wa, incident, send, monkeypatch):
    from fleet.webhook import media

    def never(mid):
        raise AssertionError("must not download media for an unknown sender")
    monkeypatch.setattr(media, "download_media", never)
    assert send(STRANGER, "image", image={"id": "m1"})["status"] == "ignored"
    assert wa.texts(MANAGER) == []


# ------------------------------------------------------------------ location
def test_driver_location_is_forwarded_to_manager(wa, active_driver, send):
    res = send(DRIVER, "location", location={"latitude": 25.6, "longitude": 85.1, "name": "NH30"})
    assert res["action"].startswith("Driver location forwarded")
    assert any("maps?q=25.6,85.1" in t for t in wa.texts(MANAGER))


def test_location_from_non_driver_is_not_forwarded(wa, incident, send):
    send(STRANGER, "location", location={"latitude": 1.0, "longitude": 2.0})
    assert wa.texts(MANAGER) == []


@pytest.mark.parametrize("loc", [{}, {"latitude": 1.0}, {"latitude": "x", "longitude": "y"}])
def test_location_without_coordinates_is_an_error(wa, active_driver, send, loc):
    assert send(DRIVER, "location", location=loc)["status"] == "error"


# ------------------------------------------------------------------ voice notes
class _FakeOrchestrator:
    seen = []

    def __init__(self, incident):
        type(self).seen.append(incident)

    def run_swarm(self):
        return None


@pytest.fixture
def voice(monkeypatch):
    from fleet.webhook import media
    _FakeOrchestrator.seen = []
    monkeypatch.setattr(media, "EnterpriseAgenticRAGOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(media, "download_media", lambda mid: b"audio")
    return media


def test_voice_note_from_driver_creates_an_incident(wa, voice, send, monkeypatch):
    monkeypatch.setattr(voice, "transcribe_voice_note", lambda b, m: {
        "vehicle_id": "UNKNOWN", "location": "Gaya", "issue_type": "engine overheating"})
    res = send(DRIVER, "audio", audio={"id": "a1", "mime_type": "audio/ogg; codecs=opus"})
    assert res["status"] == "success"
    created = _FakeOrchestrator.seen[0]
    assert (created.vehicle_id, created.location, created.issue_type) == (VEHICLE, "Gaya", "engine overheating")


def test_voice_note_from_unregistered_number_is_refused(wa, voice, send):
    assert send(STRANGER, "audio", audio={"id": "a1"})["status"] == "ignored"
    assert _FakeOrchestrator.seen == []


def test_voice_note_with_unknown_vehicle_asks_for_it(wa, voice, send, monkeypatch):
    monkeypatch.setattr(voice, "transcribe_voice_note", lambda b, m: {
        "vehicle_id": "UNKNOWN", "location": "UNKNOWN", "issue_type": "noise"})
    res = send(MANAGER, "audio", audio={"id": "a1"})     # manager is not in the driver map
    assert "vehicle ID missing" in res["action"] and _FakeOrchestrator.seen == []


def test_voice_note_oversized_fields_are_truncated(wa, voice, send, monkeypatch):
    monkeypatch.setattr(voice, "transcribe_voice_note", lambda b, m: {
        "vehicle_id": "V" * 500, "location": "L" * 5000, "issue_type": "I" * 5000})
    send(DRIVER, "audio", audio={"id": "a1"})
    created = _FakeOrchestrator.seen[0]
    assert len(created.vehicle_id) <= 40 and len(created.location) <= 300 and len(created.issue_type) <= 500


def test_voice_failures_are_reported_to_the_sender(wa, voice, send, monkeypatch):
    monkeypatch.setattr(voice, "download_media", lambda mid: None)
    assert send(DRIVER, "audio", audio={"id": "a1"})["action"] == "Voice note download failed."
    monkeypatch.setattr(voice, "download_media", lambda mid: b"x")
    monkeypatch.setattr(voice, "transcribe_voice_note", lambda b, m: None)
    assert send(DRIVER, "audio", audio={"id": "a2"})["action"] == "Voice note transcription failed."


# ------------------------------------------------------------------ robustness
def test_invalid_json_and_status_callbacks_are_harmless():
    from fleet.webhook import handle_webhook_body
    assert handle_webhook_body(b"not json")["status"] == "error"
    assert handle_webhook_body(b"\xff\xfe")["status"] == "error"
    assert handle_webhook_body(b"{}") == {"status": "received"}
    status_cb = {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]}
    assert handle_webhook_body(json.dumps(status_cb).encode()) == {"status": "received"}


@pytest.mark.parametrize("payload", [
    [], "x", {"entry": []}, {"entry": [None]}, {"entry": [{"changes": []}]},
    {"entry": [{"changes": [{"value": {"messages": ["bad"]}}]}]},
    {"entry": [{"changes": [{"value": {"messages": [{"type": "text"}]}}]}]},
])
def test_malformed_payload_shapes_never_raise(payload):
    from fleet.webhook import handle_webhook_body
    assert handle_webhook_body(json.dumps(payload).encode())["status"] in ("received", "ignored")


def test_unsupported_message_type_is_ignored(wa, send):
    assert send(STRANGER, "sticker", sticker={})["status"] == "ignored"


def test_retried_delivery_is_processed_only_once(wa, incident, press, timers):
    from fleet.webhook import handle_webhook_body
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{
        "from": digits_of(MANAGER), "id": "wamid.SAME", "type": "interactive",
        "interactive": {"button_reply": {"id": f"APPROVE_{incident}"}}}]}}]}]}).encode()
    assert handle_webhook_body(body)["status"] == "success"
    sent = len(wa.calls)
    assert handle_webhook_body(body) == {"status": "duplicate"}
    assert len(wa.calls) == sent and len(timers.instances) == 1


def test_dedupe_table_is_pruned(wa, send):
    from fleet.webhook import router
    old = 1.0
    state.PROCESSED_MESSAGES["old-id"] = old
    router._seen_since_prune = router._PRUNE_EVERY - 1
    send(STRANGER, "text", text={"body": "hi"})
    assert "old-id" not in state.PROCESSED_MESSAGES


def test_sender_flooding_is_rate_limited(wa, send):
    from fleet import auth
    from security import RateLimiter
    auth.webhook_limiter = RateLimiter(3, 60)
    import fleet.webhook.router as router
    router.webhook_limiter = auth.webhook_limiter
    results = [send(STRANGER, "text", text={"body": "hi"})["status"] for _ in range(5)]
    assert results.count("rate_limited") == 2
    assert wa.texts(MANAGER) == []
    # One noisy number does not affect another.
    assert send(MANAGER, "text", text={"body": "hi"})["status"] != "rate_limited"


def test_message_without_sender_is_ignored(wa):
    from fleet.webhook import handle_webhook_body
    body = {"entry": [{"changes": [{"value": {"messages": [{"id": "x", "type": "text", "text": {"body": "hi"}}]}}]}]}
    assert handle_webhook_body(json.dumps(body).encode())["status"] == "ignored"


def test_no_phone_number_is_logged_on_success(wa, incident, press, capsys):
    press(MANAGER, f"APPROVE_{incident}")
    out = capsys.readouterr().out
    assert digits_of(DRIVER) not in out and digits_of(MANAGER) not in out


def test_stale_context_entry_does_not_grant_chat_access(wa, incident, say):
    """E.g. the manager number was changed: the old number's context row must not keep working."""
    state.INCIDENT_CONTEXTS[last10(STRANGER)] = incident
    res = say(STRANGER, "what is the hub phone number?")
    assert res["status"] == "error"
    assert "Hub One" not in " ".join(wa.texts(STRANGER))
