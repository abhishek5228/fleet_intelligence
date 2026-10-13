"""Runtime configuration, read from the environment.

Every setting is an environment variable. An optional JSON file named by FLEET_CONFIG_FILE can
supply the three structured settings (DRIVER_WHATSAPP_MAPPING, TEST_VENDOR_NUMBERS,
TEST_HUB_NUMBERS); an environment variable of the same name wins over the file.

* Blank values count as unset, so copying .env.example verbatim is safe.
* Secure by default: the API-key check, the webhook signature check and the manager-only
  buttons refuse to run when their secret is missing. Set ALLOW_INSECURE_DEV=true to switch
  that off for local development ONLY.
* Real phone numbers and tokens never live in the source tree.
"""
import json
import os
from typing import Any, Dict, List, Mapping, Optional

from fleet.phones import last10
from security import parse_origins

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRUE = {"1", "true", "yes", "on"}


def _text(env: Mapping[str, str], name: str) -> Optional[str]:
    raw = env.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (_text(env, name) or "").lower() in _TRUE


def _number(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️ [config] {name}={raw!r} is not a whole number — using the default ({default}).")
        return default


def _read_file(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"⚠️ [config] could not read FLEET_CONFIG_FILE {path!r}: {exc}")
        return {}
    if not isinstance(data, dict):
        print("⚠️ [config] FLEET_CONFIG_FILE must contain a JSON object — ignoring it.")
        return {}
    return data


def _structured(env: Mapping[str, str], file_cfg: Mapping[str, Any], name: str) -> Any:
    """A JSON-valued setting: the environment variable first, then the config file."""
    raw = _text(env, name)
    if raw is not None:
        try:
            return json.loads(raw)
        except ValueError:
            print(f"⚠️ [config] {name} is not valid JSON — ignoring it.")
    return file_cfg.get(name)


def _phone_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v.strip() for k, v in value.items() if isinstance(v, str) and v.strip()}


def _vendor_list(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if isinstance(value, list):
        for item in value:
            phone = item.get("phone") if isinstance(item, dict) else None
            if isinstance(phone, str) and phone.strip():
                out.append({"name": str(item.get("name") or phone.strip()), "phone": phone.strip()})
    return out


def _number_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


class Settings:
    """Plain attribute bag with upper-case names, so tests can monkeypatch single values."""

    def __init__(self, env: Optional[Mapping[str, str]] = None):
        self.load(os.environ if env is None else env)

    def load(self, env: Mapping[str, str]) -> None:
        file_cfg = _read_file(_text(env, "FLEET_CONFIG_FILE"))

        # --- WhatsApp / Meta ---
        self.MANAGER_WHATSAPP_NUMBER = _text(env, "MANAGER_WHATSAPP_NUMBER") or ""
        self.WHATSAPP_TOKEN = _text(env, "WHATSAPP_TOKEN")
        self.WHATSAPP_PHONE_ID = _text(env, "PHONE_NUMBER_ID") or ""
        self.VERIFY_TOKEN = _text(env, "WHATSAPP_VERIFY_TOKEN")
        self.APP_SECRET = _text(env, "WHATSAPP_APP_SECRET")

        # --- Google ---
        self.GEMINI_API_KEY = _text(env, "GEMINI_API_KEY")
        self.GOOGLE_MAPS_API_KEY = _text(env, "GOOGLE_MAPS_API_KEY")

        # --- HTTP API protection ---
        self.FLEET_API_KEY = _text(env, "FLEET_API_KEY")
        self.ALLOWED_ORIGINS = parse_origins(env.get("ALLOWED_ORIGINS"))
        self.ALLOW_INSECURE_DEV = _flag(env, "ALLOW_INSECURE_DEV")
        self.ENABLE_DEBUG_ENDPOINTS = _flag(env, "ENABLE_DEBUG_ENDPOINTS")

        # --- Behaviour knobs ---
        self.FOLLOWUP_CHECK_DELAY_SECONDS = _number(env, "FOLLOWUP_CHECK_DELAY_SECONDS", 2 * 60 * 60)
        self.PREDICTIVE_PATTERN_WINDOW_DAYS = _number(env, "PREDICTIVE_PATTERN_WINDOW_DAYS", 90)
        self.PREDICTIVE_PATTERN_MIN_REPEATS = _number(env, "PREDICTIVE_PATTERN_MIN_REPEATS", 2)

        # --- Who is who (kept out of git: set via env or FLEET_CONFIG_FILE) ---
        self.DRIVER_WHATSAPP_MAPPING = _phone_map(_structured(env, file_cfg, "DRIVER_WHATSAPP_MAPPING"))
        self.TEST_VENDOR_NUMBERS = _vendor_list(_structured(env, file_cfg, "TEST_VENDOR_NUMBERS"))
        self.TEST_HUB_NUMBERS = _number_list(_structured(env, file_cfg, "TEST_HUB_NUMBERS"))

        # --- Storage ---
        self.DB_PATH = _text(env, "FLEET_DB_PATH") or os.path.join(BACKEND_DIR, "fleet_state.db")
        self.KNOWLEDGE_BASE_DIR = os.path.join(BACKEND_DIR, "knowledge_base")

    @property
    def DRIVER_PHONE_TO_VEHICLE(self) -> Dict[str, str]:
        """Last-10-digits phone -> vehicle id (reverse of DRIVER_WHATSAPP_MAPPING)."""
        return {last10(phone): vid for vid, phone in self.DRIVER_WHATSAPP_MAPPING.items()}

    def startup_warnings(self) -> List[str]:
        """Human-readable lines describing what is missing and what that means right now."""
        out: List[str] = []
        if self.ALLOW_INSECURE_DEV:
            out.append("⚠️ [security] ALLOW_INSECURE_DEV is ON — missing secrets will NOT block "
                       "requests. Use it for local development only, never in production.")
        blocked = "" if self.ALLOW_INSECURE_DEV else " (requests are refused until it is set)"
        if not self.FLEET_API_KEY:
            out.append("🔒 [security] FLEET_API_KEY is not set — /api/triage and "
                       f"/api/send-whatsapp-interactive are unprotected{blocked}.")
        if not self.APP_SECRET:
            out.append("🔒 [security] WHATSAPP_APP_SECRET is not set — webhook POSTs cannot be "
                       f"signature-verified{blocked}.")
        if not self.VERIFY_TOKEN:
            out.append("🔒 [security] WHATSAPP_VERIFY_TOKEN is not set — Meta's webhook handshake "
                       "will be refused.")
        if not self.MANAGER_WHATSAPP_NUMBER:
            out.append("⚠️ [config] MANAGER_WHATSAPP_NUMBER is not set — manager alerts have no "
                       f"recipient and manager-only buttons are rejected{blocked}.")
        if self.WHATSAPP_TOKEN and not self.WHATSAPP_PHONE_ID:
            out.append("⚠️ [config] WHATSAPP_TOKEN is set but PHONE_NUMBER_ID is not — "
                       "WhatsApp sends will fail.")
        if not self.DRIVER_WHATSAPP_MAPPING:
            out.append("⚠️ [config] DRIVER_WHATSAPP_MAPPING is empty — drivers cannot be notified "
                       "and voice/photo reports from drivers are not recognised.")
        return out


cfg = Settings()
