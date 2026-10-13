"""Tests for the webhook signature check, CORS parsing, and endpoint protection."""
import json

import pytest

from security import sign, sig_ok, parse_origins

SEC = "app-secret-for-tests"
BODY = json.dumps({"entry": [{"changes": [{"value": {}}]}]}).encode()


# ---------- pure helpers ----------

def test_sign_matches_meta_format():
    sig = sign(SEC, BODY)
    assert sig.startswith("sha256=") and len(sig) == len("sha256=") + 64


def test_sig_ok_accepts_valid_signature():
    assert sig_ok(SEC, BODY, sign(SEC, BODY))


def test_sig_ok_rejects_tampered_body():
    assert not sig_ok(SEC, BODY + b" ", sign(SEC, BODY))


def test_sig_ok_rejects_wrong_secret():
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


# ---------- endpoints ----------

@pytest.fixture
def cli(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main, "_rate_limit_tracker", {})
    return TestClient(main.app), main


def _post(c, body, hdr=None):
    heads = {"content-type": "application/json"}
    if hdr:
        heads["x-hub-signature-256"] = hdr
    return c.post("/api/whatsapp-webhook", content=body, headers=heads)


def test_webhook_accepts_valid_signature(cli, monkeypatch):
    c, main = cli
    monkeypatch.setattr(main, "APP_SECRET", SEC)
    r = _post(c, BODY, sign(SEC, BODY))
    assert r.status_code == 200 and r.json() == {"status": "received"}


def test_webhook_rejects_bad_signature(cli, monkeypatch):
    c, main = cli
    monkeypatch.setattr(main, "APP_SECRET", SEC)
    assert _post(c, BODY, "sha256=" + "0" * 64).status_code == 403


def test_webhook_rejects_missing_signature(cli, monkeypatch):
    c, main = cli
    monkeypatch.setattr(main, "APP_SECRET", SEC)
    assert _post(c, BODY).status_code == 403


def test_webhook_unsigned_allowed_when_secret_unset(cli, monkeypatch):
    c, main = cli
    monkeypatch.setattr(main, "APP_SECRET", None)
    assert _post(c, BODY).status_code == 200


def test_webhook_handshake(cli):
    c, main = cli
    q = {"hub.mode": "subscribe", "hub.verify_token": main.VERIFY_TOKEN, "hub.challenge": "1234"}
    r = c.get("/api/whatsapp-webhook", params=q)
    assert r.status_code == 200 and r.json() == 1234
    q["hub.verify_token"] = "wrong"
    assert c.get("/api/whatsapp-webhook", params=q).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("get", "/api/debug-pending-quotes"),
    ("post", "/api/debug-clear-pending-quotes"),
    ("get", "/api/test-gemini"),
])
def test_debug_endpoints_require_api_key(cli, monkeypatch, method, path):
    c, main = cli
    monkeypatch.setattr(main, "FLEET_API_KEY", "k3y")
    assert getattr(c, method)(path).status_code == 401
    assert getattr(c, method)(path, headers={"x-api-key": "wrong"}).status_code == 401


def test_debug_pending_quotes_with_key(cli, monkeypatch):
    c, main = cli
    monkeypatch.setattr(main, "FLEET_API_KEY", "k3y")
    r = c.get("/api/debug-pending-quotes", headers={"x-api-key": "k3y"})
    assert r.status_code == 200 and "count" in r.json()


def test_debug_api_key_does_not_leak_server_key_length(cli, monkeypatch):
    c, main = cli
    monkeypatch.setattr(main, "FLEET_API_KEY", "k3y")
    assert "fleet_api_key_length_on_server" not in c.get("/api/debug-api-key").json()


def test_health(cli):
    c, _ = cli
    assert c.get("/api/health").json()["status"] == "online"
