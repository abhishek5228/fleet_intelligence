"""Process-wide persistent state shared by every flow.

Modules read these as `state.APPROVAL_STATES` etc. (attribute access at call time), so tests
can swap in a fresh database with `init_state(path)`.

STATE_LOCK serialises every compound read-modify-write on this state: inbound webhook
messages, the quote/follow-up timer threads and the triage pipeline all take it. It is
re-entrant, so a handler that already holds it may call another that takes it. The app runs as
a single instance on SQLite, so one coarse lock is the simplest thing that is correct.
"""
import functools
import threading
from typing import Any, Callable, Optional, TypeVar

from fleet.store import PersistentDict

STATE_LOCK = threading.RLock()

_F = TypeVar("_F", bound=Callable[..., Any])


def locked(fn: _F) -> _F:
    """Run `fn` while holding STATE_LOCK (re-entrant, so locked functions may call each other)."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with STATE_LOCK:
            return fn(*args, **kwargs)
    return wrapper  # type: ignore[return-value]


APPROVAL_STATES: PersistentDict
INCIDENT_CONTEXTS: PersistentDict     # manager/driver phone -> incident_id
INCIDENT_DETAILS: PersistentDict      # incident_id -> full context dict (used for AI replies)
ACTIVE_VEHICLE_BY_PHONE: PersistentDict
PENDING_QUOTES: PersistentDict        # incident_id -> outstanding vendor quote requests
VEHICLE_ISSUE_HISTORY: PersistentDict  # vehicle_id -> [{issue_type, keywords, ts}]
DRIVER_LAST_UPDATE_TS: PersistentDict  # incident_id -> unix ts of the driver's last message
PROCESSED_MESSAGES: PersistentDict    # WhatsApp message id -> unix ts (de-duplicates Meta retries)

_TABLES = {
    "APPROVAL_STATES": "approval_states",
    "INCIDENT_CONTEXTS": "incident_contexts",
    "INCIDENT_DETAILS": "incident_details",
    "ACTIVE_VEHICLE_BY_PHONE": "active_vehicle_by_phone",
    "PENDING_QUOTES": "pending_quotes",
    "VEHICLE_ISSUE_HISTORY": "vehicle_issue_history",
    "DRIVER_LAST_UPDATE_TS": "driver_last_update_ts",
    "PROCESSED_MESSAGES": "processed_messages",
}


def init_state(db_path: Optional[str] = None) -> None:
    """(Re)create every store, optionally on another database file (used by tests)."""
    for attr, table in _TABLES.items():
        old = globals().get(attr)
        if old is not None:
            old.close()
        globals()[attr] = PersistentDict(table, db_path)


init_state()
