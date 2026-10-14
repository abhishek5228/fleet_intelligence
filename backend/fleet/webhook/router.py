"""Entry point for a verified WhatsApp webhook body."""
import json
import time
from typing import Any, Dict, Optional

from fleet import state
from fleet.auth import webhook_limiter
from fleet.webhook import buttons, chat, media
from fleet.webhook.roles import Sender
from fleet.whatsapp import send_whatsapp_text_reply

DEDUPE_TTL_SECONDS = 24 * 60 * 60
_PRUNE_EVERY = 200
_seen_since_prune = 0


def _first_message(data: Any) -> Optional[dict]:
    """Defensively dig the first message out of Meta's nested payload (None if there isn't one)."""
    try:
        messages = data["entry"][0]["changes"][0]["value"].get("messages") or []
        first = messages[0] if messages else None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return first if isinstance(first, dict) else None


def _already_processed(message_id: Optional[str]) -> bool:
    """Meta retries deliveries it thinks failed; ignore ids we have already handled.

    Called under STATE_LOCK. Marks the id as seen *before* the handler runs, so a retry that
    arrives while a slow handler is still working cannot trigger the side effects twice."""
    global _seen_since_prune
    if not message_id:
        return False
    if message_id in state.PROCESSED_MESSAGES:
        return True
    state.PROCESSED_MESSAGES[message_id] = time.time()
    _seen_since_prune += 1
    if _seen_since_prune >= _PRUNE_EVERY:
        _seen_since_prune = 0
        cutoff = time.time() - DEDUPE_TTL_SECONDS
        for mid, ts in list(state.PROCESSED_MESSAGES.items()):
            if ts < cutoff:
                del state.PROCESSED_MESSAGES[mid]
    return False


def _dispatch(msg: dict, sender: Sender) -> Dict[str, Any]:
    kind = msg.get("type")
    if kind == "audio":
        return media.handle_voice(msg, sender)
    if kind == "image":
        return media.handle_photo(msg, sender)
    if kind == "location":
        return media.handle_location(msg, sender)
    if kind == "interactive":
        return buttons.handle_button(msg, sender)
    if kind == "text":
        return chat.handle_text(msg, sender)
    return {"status": "ignored", "action": f"Unsupported message type: {kind!r}."}


def handle_webhook_body(body: bytes) -> Dict[str, Any]:
    """Process one signature-verified webhook delivery. Never raises: Meta must always get a
    200, or it retries the delivery for days."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"status": "error", "details": "Body is not valid JSON."}
    # Log size only - the payload holds phone numbers and message text (PII).
    print(f"webhook: received payload ({len(body)} bytes)")

    msg = _first_message(data)
    if msg is None:
        return {"status": "received"}  # status callbacks (delivered/read) carry no message

    sender = Sender.from_message(msg)
    try:
        with state.STATE_LOCK:
            if _already_processed(msg.get("id")):
                return {"status": "duplicate"}
            if not sender.known:
                return {"status": "ignored", "action": "Message has no sender."}
            if not webhook_limiter.allow(sender.ten):
                return {"status": "rate_limited"}
            return _dispatch(msg, sender)
    except Exception as e:  # noqa: BLE001 - a bad message must not become a 500
        print("DEBUG WEBHOOK ERROR:", type(e).__name__, str(e))
        try:
            send_whatsapp_text_reply(sender.raw, "⚠️ Kuch gadbad ho gayi, kripya thodi der baad dobara try karein.")
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "details": type(e).__name__}
