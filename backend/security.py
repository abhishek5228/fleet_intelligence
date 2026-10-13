"""Small, dependency-free security helpers (stdlib only, so they are trivially testable)."""
import hashlib
import hmac
import threading
import time
from typing import Callable, Dict, List, Optional


def safe_equals(a: Optional[str], b: Optional[str]) -> bool:
    """Constant-time string comparison. False if either side is missing or empty.

    Encodes to bytes first so a non-ASCII header value can never raise TypeError.
    """
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class RateLimiter:
    """Thread-safe sliding-window limiter keyed by an arbitrary string (client IP, sender...).

    Old keys are swept once the table grows past `max_keys`, so a stream of one-off clients
    cannot grow memory without bound.
    """

    def __init__(self, max_requests: int, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.time, max_keys: int = 10_000):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record a hit for `key`; return False (and record nothing) if it is over the limit."""
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if now - t < self.window_seconds]
            if len(hits) >= self.max_requests:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > self.max_keys:
                self._sweep(now)
            return True

    def _sweep(self, now: float) -> None:
        stale = [k for k, hits in self._hits.items()
                 if not hits or now - hits[-1] >= self.window_seconds]
        for k in stale:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


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
