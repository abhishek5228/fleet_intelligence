"""Security rules: signature check, constant-time compare, rate limiter, and fail-closed auth."""
import json

import pytest

from fleet import auth
from fleet.config import cfg
from security import RateLimiter, parse_origins, safe_equals, sig_ok, sign

SEC = "app-secret-for-tests"
BODY = json.dumps({"entry": [{"changes": [{"value": {}}]}]}).encode()


# ---------- pure helpers ----------

def test_sign_matches_meta_format():
    sig = sign(SEC, BODY)
    assert sig.startswith("sha256=") and len(sig) == len("sha256=") + 64


def test_sig_ok_accepts_valid_signature():
    assert sig_ok(SEC, BODY, sign(SEC, BODY))


def test_sig_ok_rejects_tampered_body_and_wrong_secret():
    assert not sig_ok(SEC, BODY + b" ", sign(SEC, BODY))
    assert not sig_ok("other-secret", BODY, sign(SEC, BODY))


@pytest.mark.parametrize("hdr", [None, "", "deadbeef", "sha1=abc", "sha256="])
def test_sig_ok_rejects_missing_or_malformed_header(hdr):
    assert not sig_ok(SEC, BODY, hdr)


def test_sig_ok_rejects_empty_secret():
    assert not sig_ok("", BODY, sign("", BODY))


def test_parse_origins():
    assert parse_origins(None) == ["*"]
    assert parse_origins("") == ["*"]
    assert parse_origins(" https://a.app , https://b.app ,") == ["https://a.app", "https://b.app"]


def test_safe_equals():
    assert safe_equals("abc", "abc")
    assert not safe_equals("abc", "abd")
    assert not safe_equals(None, "abc") and not safe_equals("abc", None)
    assert not safe_equals("", "")           # two empty values must NOT count as a match
    assert not safe_equals("é", "e")         # non-ASCII input must not raise


# ---------- rate limiter ----------

def test_rate_limiter_blocks_per_key_and_recovers():
    now = [1000.0]
    rl = RateLimiter(3, 60, clock=lambda: now[0])
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("b")                       # other keys unaffected
    now[0] += 61
    assert rl.allow("a")                       # window slid past the old hits


def test_rate_limiter_rejected_calls_do_not_extend_the_block():
    now = [0.0]
    rl = RateLimiter(1, 10, clock=lambda: now[0])
    assert rl.allow("a")
    for _ in range(5):
        now[0] += 1
        assert not rl.allow("a")
    now[0] = 10.5
    assert rl.allow("a")


def test_rate_limiter_sweeps_expired_keys():
    now = [0.0]
    rl = RateLimiter(5, 10, clock=lambda: now[0], max_keys=50)
    for i in range(50):
        rl.allow(f"ip-{i}")
    now[0] = 100.0
    for i in range(50, 120):
        rl.allow(f"ip-{i}")
    # Expired keys were swept; only the 70 live ones remain.
    assert len(rl._hits) == 70 and "ip-0" not in rl._hits


def test_rate_limiter_reset():
    rl = RateLimiter(1, 60)
    rl.allow("a")
    rl.reset()
    assert rl.allow("a")


# ---------- fail-closed checks ----------

def _status(fn, *args):
    with pytest.raises(auth.AuthError) as err:
        fn(*args)
    return err.value.status_code


def test_api_key_accepts_match_and_rejects_everything_else():
    auth.check_api_key("test-api-key")
    assert _status(auth.check_api_key, "wrong") == 401
    assert _status(auth.check_api_key, None) == 401
    assert _status(auth.check_api_key, "") == 401


def test_api_key_fails_closed_when_unset(monkeypatch):
    monkeypatch.setattr(cfg, "FLEET_API_KEY", None)
    assert _status(auth.check_api_key, None) == 503
    assert _status(auth.check_api_key, "anything") == 503


def test_api_key_unset_is_allowed_only_with_explicit_dev_flag(monkeypatch):
    monkeypatch.setattr(cfg, "FLEET_API_KEY", None)
    monkeypatch.setattr(cfg, "ALLOW_INSECURE_DEV", True)
    auth.check_api_key(None)


def test_webhook_signature_valid_bad_missing():
    auth.check_webhook_signature(BODY, sign(cfg.APP_SECRET, BODY))
    assert _status(auth.check_webhook_signature, BODY, "sha256=" + "0" * 64) == 403
    assert _status(auth.check_webhook_signature, BODY, None) == 403


def test_webhook_signature_fails_closed_when_secret_unset(monkeypatch):
    monkeypatch.setattr(cfg, "APP_SECRET", None)
    assert _status(auth.check_webhook_signature, BODY, None) == 503
    assert _status(auth.check_webhook_signature, BODY, "sha256=abc") == 503


def test_webhook_signature_unset_is_allowed_only_with_dev_flag(monkeypatch):
    monkeypatch.setattr(cfg, "APP_SECRET", None)
    monkeypatch.setattr(cfg, "ALLOW_INSECURE_DEV", True)
    auth.check_webhook_signature(BODY, None)


def test_handshake():
    assert auth.verify_handshake("subscribe", "verify-me", "1234") == 1234
    assert _status(auth.verify_handshake, "subscribe", "wrong", "1") == 403
    assert _status(auth.verify_handshake, "unsubscribe", "verify-me", "1") == 403
    assert _status(auth.verify_handshake, "subscribe", "verify-me", "abc") == 400
    assert _status(auth.verify_handshake, "subscribe", "verify-me", None) == 400


def test_handshake_refused_when_verify_token_unset(monkeypatch):
    monkeypatch.setattr(cfg, "VERIFY_TOKEN", None)
    assert _status(auth.verify_handshake, "subscribe", None, "1") == 403
    assert _status(auth.verify_handshake, "subscribe", "", "1") == 403


def test_rate_limit_check_is_per_ip(monkeypatch):
    monkeypatch.setattr(auth, "api_limiter", RateLimiter(3, 60))
    for _ in range(3):
        auth.check_rate_limit("1.1.1.1")
    assert _status(auth.check_rate_limit, "1.1.1.1") == 429
    auth.check_rate_limit("2.2.2.2")


def test_debug_endpoints_are_off_unless_enabled(monkeypatch):
    assert _status(auth.check_debug_enabled) == 404
    monkeypatch.setattr(cfg, "ENABLE_DEBUG_ENDPOINTS", True)
    auth.check_debug_enabled()


# ---------- HTTP layer (needs FastAPI + httpx; skipped where they are not installed) ----------

@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from fleet.api import app
    return TestClient(app)


def _post(c, body, hdr=None):
    heads = {"content-type": "application/json"}
    if hdr:
        heads["x-hub-signature-256"] = hdr
    return c.post("/api/whatsapp-webhook", content=body, headers=heads)


def test_http_webhook_signature(client):
    assert _post(client, BODY, sign(SEC, BODY)).json() == {"status": "received"}
    assert _post(client, BODY, "sha256=" + "0" * 64).status_code == 403
    assert _post(client, BODY).status_code == 403


def test_http_webhook_unsigned_is_refused_when_secret_unset(client, monkeypatch):
    monkeypatch.setattr(cfg, "APP_SECRET", None)
    assert _post(client, BODY).status_code == 503


def test_http_handshake(client):
    q = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"}
    r = client.get("/api/whatsapp-webhook", params=q)
    assert r.status_code == 200 and r.json() == 1234
    assert client.get("/api/whatsapp-webhook", params={**q, "hub.verify_token": "bad"}).status_code == 403


def test_http_debug_endpoints_hidden_by_default_and_need_key_when_enabled(client, monkeypatch):
    for method, path in (("get", "/api/debug-pending-quotes"), ("post", "/api/debug-clear-pending-quotes"),
                         ("get", "/api/test-gemini")):
        assert getattr(client, method)(path, headers={"x-api-key": "test-api-key"}).status_code == 404
    monkeypatch.setattr(cfg, "ENABLE_DEBUG_ENDPOINTS", True)
    assert client.get("/api/debug-pending-quotes").status_code == 401
    assert client.get("/api/debug-pending-quotes", headers={"x-api-key": "test-api-key"}).status_code == 200


def test_http_removed_debug_api_key_endpoint(client, monkeypatch):
    monkeypatch.setattr(cfg, "ENABLE_DEBUG_ENDPOINTS", True)
    assert client.get("/api/debug-api-key").status_code == 404


def test_http_triage_requires_key_and_fails_closed(client, monkeypatch):
    payload = {"vehicle_id": "V", "location": "L", "issue_type": "I", "severity": "S", "cargo_type": "C"}
    assert client.post("/api/triage", json=payload).status_code == 401
    monkeypatch.setattr(cfg, "FLEET_API_KEY", None)
    assert client.post("/api/triage", json=payload).status_code == 503


def test_http_triage_rejects_oversized_fields(client):
    payload = {"vehicle_id": "V", "location": "L" * 10_000, "issue_type": "I", "severity": "S", "cargo_type": "C"}
    assert client.post("/api/triage", json=payload, headers={"x-api-key": "test-api-key"}).status_code == 422


def test_http_health_and_approval_status(client):
    assert client.get("/api/health").json()["status"] == "online"
    assert client.get("/api/approval-status/INC-NOPE").json()["approval_status"] == "PENDING_MANAGER_APPROVAL"
