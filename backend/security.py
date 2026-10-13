"""Small, dependency-free security helpers (stdlib only, so they are trivially testable)."""
import hashlib
import hmac
from typing import List, Optional


def sign(sec: str, body: bytes) -> str:
    """Return Meta's 'sha256=<hex>' signature of `body` using the app secret."""
    return "sha256=" + hmac.new(sec.encode(), body, hashlib.sha256).hexdigest()


def sig_ok(sec: str, body: bytes, hdr: Optional[str]) -> bool:
    """Check Meta's X-Hub-Signature-256 header in constant time.

    False for a missing/malformed header or a wrong signature.
    """
    if not sec or not hdr or not hdr.startswith("sha256="):
        return False
    return hmac.compare_digest(sign(sec, body), hdr)


def parse_origins(raw: Optional[str]) -> List[str]:
    """Turn a comma-separated ALLOWED_ORIGINS value into a list ('*' if unset/empty)."""
    out = [o.strip() for o in (raw or "").split(",") if o.strip()]
    return out or ["*"]
