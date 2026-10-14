"""Request checks that decide who may do what — independent of the web framework.

Each check raises AuthError(status_code, detail); the FastAPI layer turns that into an
HTTPException. Keeping the rules here means they can be unit-tested without a web server.

The rules fail CLOSED: if the secret that a check depends on is not configured, the request is
refused (HTTP 503) instead of silently skipping the check. Setting ALLOW_INSECURE_DEV=true is
the one explicit, loud way to turn that off for local development.
"""
from typing import Optional

from fleet.config import cfg
from security import RateLimiter, safe_equals, sig_ok

API_RATE_LIMIT_MAX_REQUESTS = 20
API_RATE_LIMIT_WINDOW_SECONDS = 60

api_limiter = RateLimiter(API_RATE_LIMIT_MAX_REQUESTS, API_RATE_LIMIT_WINDOW_SECONDS)
# Per-sender cap on inbound WhatsApp messages: bounds Gemini/Maps spend if a number floods us.
webhook_limiter = RateLimiter(30, 60)


class AuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def check_api_key(provided: Optional[str]) -> None:
    """Guard for the dashboard-facing endpoints (X-API-Key header)."""
    expected = cfg.FLEET_API_KEY
    if not expected:
        if cfg.ALLOW_INSECURE_DEV:
            return
        raise AuthError(503, "API authentication is not configured on the server.")
    if not safe_equals(provided, expected):
        raise AuthError(401, "Invalid or missing X-API-Key header.")


def check_rate_limit(client_ip: str) -> None:
    if not api_limiter.allow(client_ip):
        raise AuthError(429, "Rate limit exceeded — too many requests, please slow down.")


def check_debug_enabled() -> None:
    """Debug/diagnostic endpoints do not exist unless ENABLE_DEBUG_ENDPOINTS=true."""
    if not cfg.ENABLE_DEBUG_ENDPOINTS:
        raise AuthError(404, "Not Found")


def check_webhook_signature(body: bytes, header: Optional[str]) -> None:
    """Verify Meta's X-Hub-Signature-256 over the raw request body."""
    secret = cfg.APP_SECRET
    if not secret:
        if cfg.ALLOW_INSECURE_DEV:
            return
        raise AuthError(503, "Webhook signature verification is not configured on the server.")
    if not sig_ok(secret, body, header):
        raise AuthError(403, "Invalid webhook signature.")


def verify_handshake(mode: Optional[str], token: Optional[str], challenge: Optional[str]) -> int:
    """Meta's one-time GET handshake. Returns the challenge number to echo back."""
    if mode != "subscribe" or not safe_equals(token, cfg.VERIFY_TOKEN):
        raise AuthError(403, "Verification token mismatch")
    try:
        return int(challenge)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise AuthError(400, "Missing or invalid hub.challenge.")
