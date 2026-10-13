"""Interactive-button replies: vendor availability, accept repair, pick vendor, approve, reject.

Button ids carry the incident id, and anyone can forge one by sending the right payload, so
every action checks WHO pressed it: manager-only buttons need the manager's number, "accept
repair" needs the incident's own driver, and a vendor button needs the vendor it was sent to.
"""
from typing import Any, Dict, Optional, Tuple

from fleet import state
from fleet.config import cfg
from fleet.directory import lookup_driver_phone, resolve_hub_phone
from fleet.followup import schedule_followup_check
from fleet.insights import build_learned_doc_from_incident
from fleet.knowledge import get_engine
from fleet.phones import last10
from fleet.quotes import notify_manager_with_quotes
from fleet.webhook.roles import Sender
from fleet.whatsapp import (
    send_driver_accept_repair_button,
    send_whatsapp_interactive_approval,
    send_whatsapp_text_reply,
)

APPROVED = "APPROVED_AND_DISPATCHED"
NO_HUB_PHONE = "Contact number not available — team will call and share shortly"


def _denied(sender: Sender, action: str) -> Dict[str, Any]:
    send_whatsapp_text_reply(sender.raw, "⛔ Aap ye action karne ke liye authorized nahi hain.")
    return {"status": "forbidden", "action": action}


def _split_id_and_vendor(remainder: str) -> Optional[Tuple[str, str]]:
    """'<incident_id>_<vendor_10_digit>' -> (incident_id, vendor_key); None if malformed."""
    if "_" not in remainder:
        return None
    inc_id, vendor_key = remainder.rsplit("_", 1)
    return (inc_id, vendor_key) if inc_id and vendor_key else None


def handle_button(msg: dict, sender: Sender) -> Dict[str, Any]:
    reply = (msg.get("interactive") or {}).get("button_reply") or {}
    payload_id = str(reply.get("id", ""))

    for prefix in ("VENDORAVAIL_YES_", "VENDORAVAIL_NO_"):
        if payload_id.startswith(prefix):
            return _vendor_availability(sender, payload_id[len(prefix):], is_yes=prefix.endswith("YES_"))
    if payload_id.startswith("ACCEPTREPAIR_"):
        return _accept_repair(sender, payload_id[len("ACCEPTREPAIR_"):])
    if payload_id.startswith("PICKVENDOR_"):
        return _pick_vendor(sender, payload_id[len("PICKVENDOR_"):])
    if payload_id.startswith("APPROVE_"):
        return _approve(sender, payload_id[len("APPROVE_"):])
    if payload_id.startswith("REJECT_"):
        return _reject(sender, payload_id[len("REJECT_"):])
    return {"status": "ignored", "action": "Unknown button."}


def _arm_followup_once(inc_id: str, incident_ctx: Dict[str, Any]) -> None:
    # Feature 5 (Proactive Follow-up) — sirf ek baar arm karo per incident.
    if not incident_ctx.get("followup_scheduled"):
        incident_ctx["followup_scheduled"] = True
        state.INCIDENT_DETAILS[inc_id] = incident_ctx
        schedule_followup_check(inc_id)


# ---------------------------------------------------------------- vendor Yes / No
def _vendor_availability(sender: Sender, remainder: str, is_yes: bool) -> Dict[str, Any]:
    parsed = _split_id_and_vendor(remainder)
    if parsed is None:
        return {"status": "ignored", "action": "Malformed vendor button."}
    inc_id, vendor_key = parsed
    if vendor_key != sender.ten:
        return _denied(sender, "Vendor button pressed by a different number.")

    quote_ctx = state.PENDING_QUOTES.get(inc_id)
    vendor = quote_ctx["vendors"].get(vendor_key) if quote_ctx else None
    if not vendor:
        send_whatsapp_text_reply(sender.raw, "⚠️ Ye enquiry ab valid nahi hai (expire ho gayi).")
        return {"status": "error", "action": "Vendor quote context not found or expired."}

    if is_yes:
        vendor["awaiting_cost"] = True
        state.PENDING_QUOTES[inc_id] = quote_ctx
        send_whatsapp_text_reply(sender.raw, "Dhanyavaad! Kripya sirf price number bhejein (jaise: 10000).")
        return {"status": "success", "action": "Vendor confirmed availability, awaiting cost."}

    vendor["status"] = "unavailable"
    vendor["awaiting_cost"] = False
    state.PENDING_QUOTES[inc_id] = quote_ctx
    send_whatsapp_text_reply(sender.raw, "Dhanyavaad, jaankari mil gayi hai.")

    all_done = all(v["status"] in ("quoted", "unavailable") for v in quote_ctx["vendors"].values())
    if all_done and not quote_ctx.get("manager_notified"):
        if not notify_manager_with_quotes(inc_id):
            send_whatsapp_text_reply(
                cfg.MANAGER_WHATSAPP_NUMBER,
                f"⚠️ None of the {len(quote_ctx['vendors'])} nearby vendors had "
                f"'{quote_ctx['part_name']}' available."
            )
            del state.PENDING_QUOTES[inc_id]
    return {"status": "success", "action": "Vendor marked unavailable."}


# ---------------------------------------------------------------- driver: Accept Repair
def _accept_repair(sender: Sender, inc_id: str) -> Dict[str, Any]:
    incident_ctx = state.INCIDENT_DETAILS.get(inc_id)
    if not incident_ctx:
        send_whatsapp_text_reply(sender.raw, "⚠️ Ye repair request ab valid nahi hai.")
        return {"status": "error", "action": "Incident not found."}
    if not sender.owns_incident(incident_ctx):
        return _denied(sender, "Accept-repair pressed by someone other than the incident's driver.")

    vehicle_id = incident_ctx.get("vehicle_id")
    state.INCIDENT_CONTEXTS[sender.ten] = inc_id
    if vehicle_id:
        state.ACTIVE_VEHICLE_BY_PHONE[sender.ten] = vehicle_id

    send_whatsapp_text_reply(
        sender.raw,
        "✅ Repair accepted! Ab aap isi chat par live updates, sawaal ya photos "
        "seedha AI Operations Agent ko bhej sakte hain."
    )
    send_whatsapp_text_reply(
        cfg.MANAGER_WHATSAPP_NUMBER,
        f"✅ Driver ({vehicle_id or 'N/A'}) ne repair accept kar liya hai — AI agent chat activate ho gayi hai."
    )
    _arm_followup_once(inc_id, incident_ctx)
    return {"status": "success", "action": "Driver accepted repair, AI agent context enabled."}


# ---------------------------------------------------------------- manager: pick a vendor
def _pick_vendor(sender: Sender, remainder: str) -> Dict[str, Any]:
    if not sender.is_manager:
        return _denied(sender, "Vendor pick by a non-manager.")
    parsed = _split_id_and_vendor(remainder)
    if parsed is None:
        return {"status": "ignored", "action": "Malformed vendor button."}
    inc_id, vendor_key = parsed

    quote_ctx = state.PENDING_QUOTES.get(inc_id)
    vendor = quote_ctx["vendors"].get(vendor_key) if quote_ctx else None
    if not vendor:
        send_whatsapp_text_reply(sender.raw, "⚠️ Ye quote ab valid nahi hai (expire ho gaya).")
        return {"status": "error", "action": "Vendor quote not found or expired."}

    incident_ctx = state.INCIDENT_DETAILS.get(inc_id, {})
    vehicle_id = incident_ctx.get("vehicle_id")
    part_name = quote_ctx["part_name"]

    # Vendor pick hi ab de-facto "approval" hai — yahin se dispatch state set karo, aur
    # driver ko breakdown+repair+vendor detail 'Accept Repair' button ke saath bhejo. AI
    # agent chat is button ke tap par hi officially enable hoga (_accept_repair).
    state.APPROVAL_STATES[inc_id] = APPROVED
    driver_phone = lookup_driver_phone(vehicle_id)
    if driver_phone:
        driver_body_text = (
            f"✅ *Repair & Parts Confirmed*\n\n"
            f"🚜 *Vehicle:* {vehicle_id}\n"
            f"🛠️ *Issue:* {incident_ctx.get('issue_type', 'N/A')}\n"
            f"📍 *Your Location:* {incident_ctx.get('location', 'N/A')}\n\n"
            f"🏢 *Nearest Service Center:* {incident_ctx.get('hub', 'Nearest Service Center')}\n"
            f"📞 *Hub Contact:* {incident_ctx.get('hub_phone') or NO_HUB_PHONE}\n\n"
            f"⚙️ *Part:* {part_name}\n"
            f"🏭 *Vendor:* {vendor['hub_name']}\n"
            f"💰 *Approved Price:* ₹{vendor['price']:.0f}\n\n"
            f"Neeche 'Accept Repair' dabaayein taaki AI agent chat activate ho jaaye "
            f"aur aap isi par live updates/photos share kar sakein."
        )
        result = send_driver_accept_repair_button(driver_phone, inc_id, driver_body_text)
        # Both a non-200 response AND an exception (status == "error") need the text fallback.
        send_failed = isinstance(result, dict) and (
            result.get("delivery_failed") or result.get("status") == "error"
        )
        if send_failed:
            send_whatsapp_text_reply(driver_phone, driver_body_text)
            driver_status_note = f"\n\n📲 Driver ({vehicle_id}, {driver_phone}): button fail hua, plain text fallback bheja gaya."
        else:
            driver_status_note = f"\n\n📲 Driver ({vehicle_id}, {driver_phone}) ko 'Accept Repair' button bhej diya gaya."
    else:
        driver_status_note = (
            f"\n\n⚠️ Driver ko notify NAHI kiya ja saka — vehicle_id={vehicle_id!r} "
            f"DRIVER_WHATSAPP_MAPPING me kisi bhi key se match nahi hua."
        )

    send_whatsapp_text_reply(
        sender.raw,
        f"✅ Confirmed: {vendor['hub_name']} — ₹{vendor['price']:.0f} for {part_name}.{driver_status_note}"
    )
    del state.PENDING_QUOTES[inc_id]
    return {"status": "success", "action": f"Vendor {vendor['hub_name']} confirmed, driver notified."}


# ---------------------------------------------------------------- manager: Approve
def _approve(sender: Sender, inc_id: str) -> Dict[str, Any]:
    if not sender.is_manager:
        return _denied(sender, "Approve by a non-manager.")
    incident_ctx = state.INCIDENT_DETAILS.get(inc_id)
    if not incident_ctx:
        send_whatsapp_text_reply(sender.raw, "⚠️ Ye incident ab valid nahi hai.")
        return {"status": "error", "action": "Incident not found."}
    if state.APPROVAL_STATES.get(inc_id) == APPROVED:
        send_whatsapp_text_reply(sender.raw, "ℹ️ Ye repair pehle hi approve ho chuka hai.")
        return {"status": "success", "action": "Already approved."}

    state.APPROVAL_STATES[inc_id] = APPROVED
    send_whatsapp_text_reply(sender.raw, "✅ Repair approved successfully by management. Dispatch sequence is active.")

    vehicle_id = incident_ctx.get("vehicle_id")
    hub = incident_ctx.get("hub")
    recommended_part = incident_ctx.get("recommended_part")

    # Self-Improving Knowledge Base: this diagnosis was Gemini's best guess (no manual
    # matched it) — now that a human has approved it, learn it permanently.
    if incident_ctx.get("hallucination_grade") == "AI_GENERATED_FALLBACK":
        try:
            learned = get_engine().add_learned_document(build_learned_doc_from_incident(incident_ctx))
            if learned:
                send_whatsapp_text_reply(
                    cfg.MANAGER_WHATSAPP_NUMBER,
                    f"🧠 Knowledge base updated: learned '{incident_ctx.get('issue_type')}' → "
                    f"{recommended_part}. Future similar reports will match this directly."
                )
        except Exception as e:  # noqa: BLE001 - learning is best-effort, approval already succeeded
            print(f"DEBUG SELF-LEARNING ERROR: {type(e).__name__}: {e}")

    driver_phone = lookup_driver_phone(vehicle_id)
    if driver_phone:
        driver_10 = last10(driver_phone)
        state.INCIDENT_CONTEXTS[driver_10] = inc_id
        state.ACTIVE_VEHICLE_BY_PHONE[driver_10] = vehicle_id
        send_whatsapp_text_reply(
            driver_phone,
            f"🚨 *OFFICIAL FLEET DISPATCH ALERT*\n\n"
            f"🚜 *Vehicle:* {vehicle_id}\n"
            f"Status: Repair *APPROVED* by Management.\n"
            f"🛠️ *Assigned Workshop:* {hub}\n"
            f"⚙️ *Required Part:* {recommended_part}\n\n"
            f"You can now reply directly on this chat with your live updates or images for the AI Operations Agent."
        )

    _arm_followup_once(inc_id, incident_ctx)
    return {"status": "success", "action": "Approved by manager and driver notified."}


# ---------------------------------------------------------------- manager: Reject
def _reject(sender: Sender, inc_id: str) -> Dict[str, Any]:
    if not sender.is_manager:
        return _denied(sender, "Reject by a non-manager.")
    incident_ctx = state.INCIDENT_DETAILS.get(inc_id)
    if not incident_ctx:
        send_whatsapp_text_reply(sender.raw, "⚠️ Ye incident ab valid nahi hai.")
        return {"status": "error", "action": "Incident not found."}
    if state.APPROVAL_STATES.get(inc_id) == APPROVED:
        send_whatsapp_text_reply(sender.raw, "ℹ️ Ye repair pehle hi approve ho chuka hai, ab reject nahi ho sakta.")
        return {"status": "error", "action": "Cannot reject an approved repair."}

    hub_records = incident_ctx.get("hub_records", [])
    next_index = incident_ctx.get("hub_index", 0) + 1

    if next_index < len(hub_records):
        # Auto-advance to the next nearest hub and re-open the approval gate for it —
        # repeats on every reject until hubs run out.
        next_hub = hub_records[next_index]
        incident_ctx["hub"] = next_hub["name"]
        incident_ctx["hub_phone"] = resolve_hub_phone(next_hub, next_index)
        incident_ctx["hub_waypoint"] = next_hub.get("waypoint")
        incident_ctx["hub_index"] = next_index
        state.INCIDENT_DETAILS[inc_id] = incident_ctx
        state.APPROVAL_STATES[inc_id] = "PENDING_MANAGER_APPROVAL"

        send_whatsapp_text_reply(
            sender.raw,
            f"❌ Rejected. 🔄 Auto-rerouting to next nearest service center "
            f"({next_index + 1}/{len(hub_records)})..."
        )
        send_whatsapp_interactive_approval(
            incident_id=inc_id,
            to_phone=sender.raw,
            vehicle_id=incident_ctx.get("vehicle_id", ""),
            hub=next_hub["name"],
            location=incident_ctx.get("location", "N/A"),
            issue_type=incident_ctx.get("issue_type", "N/A"),
            severity=incident_ctx.get("severity", "N/A"),
            cargo_type=incident_ctx.get("cargo_type", "N/A"),
            recommended_part=incident_ctx.get("recommended_part", "Standard Spare Kit"),
            predictive_alert=incident_ctx.get("predictive_alert"),
        )
        return {"status": "success", "action": f"Auto-rerouted to next hub ({next_index + 1}/{len(hub_records)}).",
                "new_hub": next_hub["name"]}

    state.APPROVAL_STATES[inc_id] = "REJECTED_REROUTING"
    send_whatsapp_text_reply(
        sender.raw,
        "❌ Rejected — no more nearby authorized service centers were found in range. "
        "Please advise manually or expand the search area."
    )
    return {"status": "success", "action": "Rejected — hub list exhausted, manual intervention needed."}
