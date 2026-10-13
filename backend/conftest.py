"""Shared test setup.

* Puts backend/ on sys.path so `import fleet` / `import security` work from anywhere.
* Points the app at a throw-away database and strips every real secret from the environment
  BEFORE `fleet.config` is first imported, so a developer's .env can never leak into a test.
* Every test starts with fresh state, no network, no real timers, and a recorder standing in
  for the WhatsApp Cloud API.
"""
import itertools
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_BOOT_DIR = tempfile.mkdtemp(prefix="fleet-tests-")
for _var in ("WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
             "GEMINI_API_KEY", "GOOGLE_MAPS_API_KEY", "FLEET_API_KEY", "ALLOWED_ORIGINS",
             "ALLOW_INSECURE_DEV", "ENABLE_DEBUG_ENDPOINTS", "MANAGER_WHATSAPP_NUMBER",
             "DRIVER_WHATSAPP_MAPPING", "TEST_VENDOR_NUMBERS", "TEST_HUB_NUMBERS",
             "FLEET_CONFIG_FILE", "FOLLOWUP_CHECK_DELAY_SECONDS"):
    os.environ.pop(_var, None)
os.environ["FLEET_DB_PATH"] = os.path.join(_BOOT_DIR, "boot.db")

# Obviously fake numbers (the 555-01xx range is reserved for fiction).
MANAGER = "+15555550100"
DRIVER = "+15555550101"
VENDOR_A = "+15555550102"
VENDOR_B = "+15555550103"
STRANGER = "+15555550199"
VEHICLE = "TEST01"


def digits_of(phone):
    return "".join(c for c in phone if c.isdigit())


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"messages": [{"id": "wamid.fake"}]}
        self.text = json.dumps(self._payload)
        self.content = self.text.encode()
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class WhatsAppRecorder:
    """Stands in for requests.post: records every outbound WhatsApp API call."""

    def __init__(self):
        self.calls = []          # [(url, json_body)]
        self.status_code = 200   # flip to simulate Meta rejecting sends
        self.raise_exc = None

    def post(self, url, json=None, headers=None, timeout=None, **_):
        if self.raise_exc:
            raise self.raise_exc
        self.calls.append((url, json))
        return FakeResponse(self.status_code)

    # ---- inspection helpers ----
    def to(self, phone):
        want = digits_of(phone)
        return [b for _, b in self.calls if b and b.get("to") == want]

    def texts(self, phone):
        return [b["text"]["body"] for b in self.to(phone) if b.get("type") == "text"]

    def interactive(self, phone):
        return [b["interactive"] for b in self.to(phone) if b.get("type") == "interactive"]

    def button_ids(self, phone):
        return [btn["reply"]["id"] for i in self.interactive(phone) for btn in i["action"]["buttons"]]

    def all_text(self):
        return "\n".join(
            b["text"]["body"] if b.get("type") == "text" else json_dumps(b.get("interactive", {}))
            for _, b in self.calls if b
        )

    def clear(self):
        self.calls.clear()


def json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


class FakeTimer:
    """threading.Timer that never runs by itself; tests call .fire()."""
    instances = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval, self.function = interval, function
        self.args, self.kwargs = list(args or []), dict(kwargs or {})
        self.daemon = False
        self.started = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.started = False

    def fire(self):
        self.function(*self.args, **self.kwargs)


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    """Fresh database, known config, no network, no real timers."""
    import requests
    import threading

    from fleet import auth, knowledge, state
    from fleet.config import cfg

    state.init_state(str(tmp_path / "state.db"))
    auth.api_limiter.reset()
    auth.webhook_limiter.reset()
    knowledge.set_engine(None)

    def _no_network(*a, **k):
        raise AssertionError("test tried to use the real network")
    monkeypatch.setattr(requests.sessions.Session, "request", _no_network)

    FakeTimer.instances = []
    monkeypatch.setattr(threading, "Timer", FakeTimer)

    for name, value in {
        "MANAGER_WHATSAPP_NUMBER": MANAGER, "WHATSAPP_TOKEN": None, "WHATSAPP_PHONE_ID": "1234567890",
        "VERIFY_TOKEN": "verify-me", "APP_SECRET": "app-secret-for-tests", "GEMINI_API_KEY": None,
        "GOOGLE_MAPS_API_KEY": None, "FLEET_API_KEY": "test-api-key", "ALLOW_INSECURE_DEV": False,
        "ENABLE_DEBUG_ENDPOINTS": False, "DRIVER_WHATSAPP_MAPPING": {VEHICLE: DRIVER},
        "TEST_VENDOR_NUMBERS": [], "TEST_HUB_NUMBERS": [],
        "FOLLOWUP_CHECK_DELAY_SECONDS": 7200,
    }.items():
        monkeypatch.setattr(cfg, name, value)
    yield
    knowledge.set_engine(None)


@pytest.fixture
def wa(monkeypatch):
    """A live-configured WhatsApp client whose HTTP calls are captured by a recorder."""
    import requests

    from fleet.config import cfg
    rec = WhatsAppRecorder()
    monkeypatch.setattr(cfg, "WHATSAPP_TOKEN", "test-token")
    monkeypatch.setattr(requests, "post", rec.post)
    return rec


@pytest.fixture
def timers():
    return FakeTimer


_ids = itertools.count(1)


@pytest.fixture
def send():
    """send(sender_phone, 'text', text={...}) -> result of the webhook handler."""
    from fleet.webhook import handle_webhook_body

    def _send(sender, msg_type, mid=None, **fields):
        msg = {"from": digits_of(sender), "id": mid or f"wamid.{next(_ids)}", "type": msg_type, **fields}
        body = {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}
        return handle_webhook_body(json.dumps(body).encode())
    return _send


@pytest.fixture
def press(send):
    def _press(sender, button_id):
        return send(sender, "interactive", interactive={"button_reply": {"id": button_id, "title": "x"}})
    return _press


@pytest.fixture
def say(send):
    def _say(sender, text):
        return send(sender, "text", text={"body": text})
    return _say


HUBS = [
    {"name": "Hub One", "phone": "+15555550111", "waypoint": "w1"},
    {"name": "Hub Two", "phone": "+15555550112", "waypoint": "w2"},
]


@pytest.fixture
def incident():
    """Seed a pending incident the way the triage pipeline would; returns its id."""
    from fleet import state
    inc = "INC-TEST0001"
    state.APPROVAL_STATES[inc] = "PENDING_MANAGER_APPROVAL"
    state.INCIDENT_CONTEXTS[MANAGER] = inc
    state.INCIDENT_DETAILS[inc] = {
        "vehicle_id": VEHICLE, "issue_type": "radiator leak", "location": "Patna",
        "severity": "HIGH", "cargo_type": "Grain", "hub": HUBS[0]["name"], "hub_phone": HUBS[0]["phone"],
        "hub_records": [dict(h) for h in HUBS], "hub_index": 0,
        "recommended_part": "Radiator hose", "hallucination_grade": "GROUNDED",
        "diagnostic_summary": "Hose cracked", "predictive_alert": None, "followup_scheduled": False,
    }
    return inc
