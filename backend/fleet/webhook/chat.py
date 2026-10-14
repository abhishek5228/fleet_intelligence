"""Free-text messages: vendor price replies, then manager/driver chat with the AI agent."""
import time
from typing import Any, Dict

from fleet import state
from fleet.config import cfg
from fleet.gemini import call_ai_agent
from fleet.quotes import handle_vendor_quote_reply
from fleet.webhook.roles import Sender
from fleet.whatsapp import send_whatsapp_text_reply

MAX_TEXT_CHARS = 2000


def handle_text(msg: dict, sender: Sender) -> Dict[str, Any]:
    text_body = str((msg.get("text") or {}).get("body", ""))[:MAX_TEXT_CHARS]

    # First: is this a reply from a spare-part vendor we sent a quote request to?
    vendor_replies = [inc_id for inc_id, ctx in list(state.PENDING_QUOTES.items())
                      if sender.ten in ctx["vendors"]]
    if vendor_replies:
        if any(handle_vendor_quote_reply(inc_id, sender.ten, text_body) for inc_id in vendor_replies):
            send_whatsapp_text_reply(sender.raw, "Dhanyavaad, jaankari mil gayi hai.")
            return {"status": "success", "action": "Vendor quote reply processed."}
        send_whatsapp_text_reply(sender.raw, "Kripya upar diye gaye Yes/No button par tap karein.")
        return {"status": "success", "action": "Vendor text ignored - awaiting button press."}

    # Only the manager and drivers of an active repair may talk to the AI agent (it answers
    # from incident details). Strangers get a refusal, never "the most recent incident".
    matched_inc_id = sender.incident_id() if (sender.is_manager or sender.is_active_driver) else None
    if not matched_inc_id:
        send_whatsapp_text_reply(sender.raw, "⚠️ No active fleet incident context found for your session.")
        return {"status": "error", "action": "No active incident context found."}

    incident_ctx = state.INCIDENT_DETAILS.get(matched_inc_id, {})
    is_driver = sender.is_active_driver
    vehicle_id = state.ACTIVE_VEHICLE_BY_PHONE.get(sender.ten, incident_ctx.get("vehicle_id", "N/A"))
    if is_driver:
        incident_ctx["current_chatter"] = f"Driver of {vehicle_id}"
        # Feature 5: har real driver message par timestamp refresh karo, taaki pending
        # follow-up timer ko pata chale driver already update de chuka hai.
        state.DRIVER_LAST_UPDATE_TS[matched_inc_id] = time.time()

    ai_result = call_ai_agent(text_body, incident_ctx)
    send_whatsapp_text_reply(sender.raw, ai_result["reply_text"])

    # Driver ka status update manager ko driver ke apne words me forward karo.
    if is_driver and ai_result.get("decision") == "DRIVER_STATUS_UPDATE":
        send_whatsapp_text_reply(
            cfg.MANAGER_WHATSAPP_NUMBER,
            f"📍 *Driver Update — {vehicle_id}*\n\n\"{text_body}\""
        )

    # Feature 4 (Urgency Detection): URGENT/CRITICAL ho to standalone escalation bhejo,
    # DRIVER_STATUS_UPDATE forward se independent.
    if is_driver and ai_result.get("urgency") in ("URGENT", "CRITICAL"):
        icon = "🆘" if ai_result["urgency"] == "CRITICAL" else "⚠️"
        send_whatsapp_text_reply(
            cfg.MANAGER_WHATSAPP_NUMBER,
            f"{icon} *{ai_result['urgency']} ALERT — {vehicle_id}*\n\n"
            f"Driver message: \"{text_body}\"\n\n"
            f"Turant dhyaan dein / call karein."
        )

    return {"status": "success", "action": "Smart AI agent replied to chatter.",
            "decision": ai_result["decision"], "urgency": ai_result.get("urgency", "NORMAL")}
