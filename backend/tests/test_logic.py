"""Tests for the pure decision logic behind the webhook (no network, no paid APIs)."""
import time

import pytest

import main


# ---------- keyword extraction ----------

def test_keywords_drop_stopwords_and_short_words():
    kws = main._extract_keywords("The clutch is overheating and has a leak")
    assert "clutch" in kws and "overheating" in kws and "leak" in kws
    for bad in ("the", "and", "has", "is"):
        assert bad not in kws


def test_keywords_dedupe_and_cap_at_limit():
    words = ["leak", "leak", "leak"] + [c * 3 for c in "abcdefghijkl"]
    kws = main._extract_keywords(" ".join(words))
    assert kws.count("leak") == 1
    assert len(kws) == 10


def test_keywords_fall_back_to_raw_text_when_nothing_matches():
    assert main._extract_keywords("12") == ["12"]


# ---------- per-vehicle predictive maintenance ----------

@pytest.fixture
def hist(monkeypatch):
    store = {}
    monkeypatch.setattr(main, "VEHICLE_ISSUE_HISTORY", store)
    monkeypatch.setattr(main, "PREDICTIVE_PATTERN_MIN_REPEATS", 2)
    monkeypatch.setattr(main, "PREDICTIVE_PATTERN_WINDOW_DAYS", 90)
    return store


def test_alert_fires_only_after_enough_repeats(hist):
    rec = main._record_and_check_vehicle_pattern
    assert rec("BR01", "radiator leak") is None
    assert rec("BR01", "radiator overheating") is None  # only 1 earlier match
    msg = rec("BR01", "radiator hose burst")             # 2 earlier matches
    assert msg and "BR01" in msg and "Predictive Maintenance" in msg


def test_unrelated_issue_does_not_alert(hist):
    rec = main._record_and_check_vehicle_pattern
    rec("BR01", "radiator leak")
    rec("BR01", "radiator leak")
    assert rec("BR01", "flat tyre") is None


def test_other_vehicles_are_isolated(hist):
    rec = main._record_and_check_vehicle_pattern
    rec("BR01", "radiator leak")
    rec("BR01", "radiator leak")
    assert rec("BR02", "radiator leak") is None


def test_old_incidents_outside_window_are_ignored(hist):
    old = {"issue_type": "radiator leak", "keywords": ["radiator", "leak"], "ts": time.time() - 200 * 86400}
    hist["BR03"] = [dict(old), dict(old), dict(old)]
    assert main._record_and_check_vehicle_pattern("BR03", "radiator leak") is None


def test_missing_vehicle_id_returns_none(hist):
    assert main._record_and_check_vehicle_pattern(None, "radiator leak") is None


# ---------- self-learning knowledge base ----------

def test_learned_doc_is_stable_and_uses_context():
    ctx = {"issue_type": "steering oil leak", "recommended_part": "Power steering hose",
           "diagnostic_summary": "Hose cracked"}
    one = main._build_learned_doc_from_incident(ctx)
    two = main._build_learned_doc_from_incident(dict(ctx))
    assert one == two
    assert one["id"].startswith("AUTOLEARN-")
    assert one["part_code"] == "Power steering hose"
    assert one["content"] == "Hose cracked"
    assert "steering" in one["keywords"]


def test_learned_doc_defaults_when_context_is_thin():
    doc = main._build_learned_doc_from_incident({"issue_type": "noise"})
    assert doc["part_code"].startswith("Standard Certified")
    assert doc["content"] == "noise"


# ---------- keyword fallback (used when the AI service is down) ----------

def _decision(text):
    return main._keyword_fallback(text)["decision"]


def test_fallback_approve_reject_and_local():
    assert _decision("approve") == "APPROVED_AND_DISPATCHED"
    assert _decision("OK") == "APPROVED_AND_DISPATCHED"
    assert _decision("haan kardo") == "APPROVED_AND_DISPATCHED"
    assert _decision("please reject this") == "REJECTED_REROUTING"
    assert _decision("mat bhejo") == "REJECTED_REROUTING"
    assert _decision("send to local mechanic") == "APPROVED_LOCAL_MECHANIC_REROUTED"


def test_fallback_does_not_match_inside_other_words():
    # Regression: "ok" used to match inside "broken", "mat" inside "material".
    assert _decision("engine is broken").startswith("CUSTOM_INSTRUCTION_LOGGED")
    assert _decision("material shortage").startswith("CUSTOM_INSTRUCTION_LOGGED")


def test_fallback_driver_status_update():
    assert _decision("on my way to the hub") == "DRIVER_STATUS_UPDATE"
    assert _decision("pahunch gaya") == "DRIVER_STATUS_UPDATE"


def test_fallback_flags_emergencies_as_critical():
    res = main._keyword_fallback("accident ho gaya")
    assert res["urgency"] == "CRITICAL"
    assert res["reply_text"].startswith("🆘")
    assert main._keyword_fallback("flat tyre")["urgency"] == "NORMAL"


# ---------- plumbing ----------

def test_whatsapp_reply_is_simulated_without_token(monkeypatch):
    monkeypatch.setattr(main, "WHATSAPP_TOKEN", None)
    assert main.send_whatsapp_text_reply("+910000000000", "hi") == {"status": "simulated_reply", "text": "hi"}


def test_require_api_key_unit(monkeypatch):
    monkeypatch.setattr(main, "FLEET_API_KEY", "k3y")
    assert main.require_api_key("k3y") is True
    with pytest.raises(main.HTTPException) as err:
        main.require_api_key("nope")
    assert err.value.status_code == 401
    monkeypatch.setattr(main, "FLEET_API_KEY", None)
    assert main.require_api_key(None) is True


def test_rate_limit_blocks_per_ip(monkeypatch):
    monkeypatch.setattr(main, "_rate_limit_tracker", {})
    monkeypatch.setattr(main, "RATE_LIMIT_MAX_REQUESTS", 3)

    class Req:
        def __init__(self, ip):
            self.client = type("C", (), {"host": ip})()

    for _ in range(3):
        assert main.rate_limit(Req("1.1.1.1")) is True
    with pytest.raises(main.HTTPException) as err:
        main.rate_limit(Req("1.1.1.1"))
    assert err.value.status_code == 429
    assert main.rate_limit(Req("2.2.2.2")) is True
