"""Voice notes, photos and shared locations."""
import time
from typing import Any, Dict

from fleet import state
from fleet.config import cfg
from fleet.gemini import diagnose_incident_photo, transcribe_voice_note
from fleet.models import IncidentInput
from fleet.orchestrator import EnterpriseAgenticRAGOrchestrator
from fleet.webhook.roles import Sender
from fleet.whatsapp import download_media, forward_image, send_whatsapp_text_reply

_NOT_REGISTERED = "⚠️ Aapka number fleet me registered nahi hai. Kripya apne manager se sampark karein."


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


# ==========================================
# VOICE NOTE SUPPORT — driver bolke breakdown report kar sake, type kiye
# bina. WhatsApp audio message ko Meta se download karke Gemini se transcribe
# + structured fields (vehicle_id, location, issue_type) extract karta hai.
# ==========================================
def handle_voice(msg: dict, sender: Sender) -> Dict[str, Any]:
    if not (sender.is_driver or sender.is_manager):
        send_whatsapp_text_reply(sender.raw, _NOT_REGISTERED)
        return {"status": "ignored", "action": "Voice note from an unregistered number."}

    audio = msg.get("audio") or {}
    media_id = audio.get("id")
    mime_type = str(audio.get("mime_type", "audio/ogg")).split(";")[0]
    audio_bytes = download_media(media_id) if media_id else None

    if not audio_bytes:
        send_whatsapp_text_reply(sender.raw, "⚠️ Voice note download nahi ho paya, kripya dobara bhejein ya type karke bhejein.")
        return {"status": "error", "action": "Voice note download failed."}

    transcribed = transcribe_voice_note(audio_bytes, mime_type)
    if not transcribed:
        send_whatsapp_text_reply(sender.raw, "⚠️ Voice note samajh nahi paya, kripya thoda clear bol ke dobara bhejein.")
        return {"status": "error", "action": "Voice note transcription failed."}

    issue_type = _clip(transcribed.get("issue_type"), 500)
    location = _clip(transcribed.get("location"), 300)
    vehicle_id = _clip(transcribed.get("vehicle_id"), 40) or "UNKNOWN"
    if vehicle_id == "UNKNOWN":
        vehicle_id = cfg.DRIVER_PHONE_TO_VEHICLE.get(sender.ten, "UNKNOWN")

    if vehicle_id == "UNKNOWN":
        send_whatsapp_text_reply(
            sender.raw,
            f"🎙️ Sunaayi diya: \"{issue_type}\"\n\n"
            f"Vehicle number pata nahi chala — kripya vehicle number bhi bol/likh ke bhejein."
        )
        return {"status": "success", "action": "Voice note transcribed, vehicle ID missing."}

    voice_incident = IncidentInput(
        vehicle_id=vehicle_id,
        location=location if location and location != "UNKNOWN" else "Location not specified",
        issue_type=issue_type or "Not specified",
        severity="MODERATE",
        cargo_type="Not specified",
    )
    send_whatsapp_text_reply(
        sender.raw,
        f"🎙️ Voice note samajh liya:\n"
        f"🚜 Vehicle: {vehicle_id}\n"
        f"📍 Location: {voice_incident.location}\n"
        f"🛠️ Issue: {voice_incident.issue_type}\n\n"
        f"Processing shuru ho gaya hai, manager ko turant alert milega."
    )
    EnterpriseAgenticRAGOrchestrator(voice_incident).run_swarm()
    return {"status": "success", "action": "Incident auto-created from voice note."}


# ==========================================
# Features 1 & 2: IN-CHAT PHOTO DIAGNOSIS + SELF-SERVICE REPAIR GUIDANCE —
# driver kabhi bhi chat me photo bhej sakta hai, AI turant analyze karke jawab deta hai.
# ==========================================
def handle_photo(msg: dict, sender: Sender) -> Dict[str, Any]:
    if not (sender.is_active_driver or sender.is_manager):
        send_whatsapp_text_reply(sender.raw, "⚠️ No active fleet incident context found for your session.")
        return {"status": "ignored", "action": "Photo from a sender with no active repair."}

    image = msg.get("image") or {}
    media_id = image.get("id")
    mime_type = str(image.get("mime_type", "image/jpeg")).split(";")[0]
    image_bytes = download_media(media_id) if media_id else None

    if not image_bytes:
        send_whatsapp_text_reply(sender.raw, "⚠️ Photo download nahi ho payi, kripya dobara bhejein.")
        return {"status": "error", "action": "Image download failed."}

    # Only the sender's OWN incident - never "whichever incident was created last".
    matched_inc_id = sender.incident_id()
    incident_ctx = state.INCIDENT_DETAILS.get(matched_inc_id, {}) if matched_inc_id else {}
    vehicle_id = state.ACTIVE_VEHICLE_BY_PHONE.get(sender.ten, incident_ctx.get("vehicle_id", "N/A"))

    if not cfg.GEMINI_API_KEY:
        # Vision analysis unavailable — driver ko turant confirm karo (kabhi silent drop
        # nahi), manager ko manual-review ke liye flag karo.
        send_whatsapp_text_reply(
            sender.raw,
            "📸 Photo mil gayi, team ko forward kar diya gaya hai — jald hi update milega."
        )
        if matched_inc_id:
            send_whatsapp_text_reply(
                cfg.MANAGER_WHATSAPP_NUMBER,
                f"📸 *Photo received from driver — {vehicle_id}*\n\n"
                f"(AI vision analysis unavailable — GEMINI_API_KEY not set. Please review manually.)"
            )
            forward_image(cfg.MANAGER_WHATSAPP_NUMBER, media_id)
        return {"status": "success", "action": "Image received, AI vision unavailable, forwarded raw."}

    diagnosis = diagnose_incident_photo(image_bytes, mime_type, incident_ctx)
    if not diagnosis:
        send_whatsapp_text_reply(
            sender.raw,
            "⚠️ Photo analyze nahi ho payi. Kripya thoda clear/roshni me dobara photo bhejein, "
            "ya seedha text me bata dein."
        )
        return {"status": "error", "action": "Photo diagnosis failed."}

    if matched_inc_id:
        # Feature 5: photo bhejna bhi ek genuine driver update hai.
        state.DRIVER_LAST_UPDATE_TS[matched_inc_id] = time.time()

    reply_lines = [f"📸 *AI Photo Diagnosis*\n{diagnosis['driver_message']}"]
    if diagnosis.get("can_self_fix") and diagnosis.get("diy_steps"):
        reply_lines.append("\n🔧 *Khud karne ke steps:*")
        for i, step in enumerate(diagnosis["diy_steps"], 1):
            reply_lines.append(f"{i}. {step}")
        reply_lines.append("\nAgar isse thik na ho ya asuraksha lage, turant bata dein — hum mechanic bhej denge.")
    send_whatsapp_text_reply(sender.raw, "\n".join(reply_lines))

    severity = diagnosis.get("severity", "MODERATE")
    icon = {"CRITICAL": "🆘", "HIGH": "🚨"}.get(severity, "📸")
    manager_lines = [
        f"{icon} *Driver photo — {vehicle_id}* (Severity: {severity})",
        f"AI diagnosis: {diagnosis.get('visible_damage', 'N/A')}",
    ]
    if diagnosis.get("can_self_fix"):
        manager_lines.append("Driver ko self-fix (DIY) steps de diye gaye hain — mechanic dispatch abhi zaroori nahi.")
    else:
        manager_lines.append(
            f"Professional help chahiye — recommended: {diagnosis.get('recommended_part') or 'inspection'}."
        )
    send_whatsapp_text_reply(cfg.MANAGER_WHATSAPP_NUMBER, "\n".join(manager_lines))
    # Best-effort: driver ki actual photo bhi manager ko forward karne ki koshish karo.
    forward_image(cfg.MANAGER_WHATSAPP_NUMBER, media_id)

    # Feature 4 (Urgency): HIGH/CRITICAL photo severity ko incident record par bhi
    # turant reflect karo, aur CRITICAL ho to standalone emergency escalation bhejo.
    if matched_inc_id and severity in ("HIGH", "CRITICAL"):
        incident_ctx["severity"] = severity
        state.INCIDENT_DETAILS[matched_inc_id] = incident_ctx
    if severity == "CRITICAL":
        send_whatsapp_text_reply(
            cfg.MANAGER_WHATSAPP_NUMBER,
            f"🆘 *CRITICAL ALERT — {vehicle_id}*\n\nPhoto se critical damage/safety risk dikh raha hai. Turant dhyaan dein / call karein."
        )

    return {"status": "success", "action": "Photo diagnosed and forwarded.", "severity": severity,
            "can_self_fix": diagnosis.get("can_self_fix", False)}


def handle_location(msg: dict, sender: Sender) -> Dict[str, Any]:
    # WhatsApp ka native "Share Location" — lat/long yahin aata hai, extraction nahi chahiye.
    loc = msg.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    place_name = _clip(loc.get("name") or loc.get("address") or "Shared location", 200)

    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        send_whatsapp_text_reply(sender.raw, "⚠️ Location samajh nahi paya, kripya dobara share karein.")
        return {"status": "error", "action": "Location message missing coordinates."}

    if not sender.is_active_driver:
        send_whatsapp_text_reply(sender.raw, "📍 Location receive ho gaya.")
        return {"status": "success", "action": "Location received (sender not an active driver)."}

    vehicle_id = state.ACTIVE_VEHICLE_BY_PHONE.get(sender.ten, "N/A")
    inc_id = state.INCIDENT_CONTEXTS.get(sender.ten)
    if inc_id:
        state.DRIVER_LAST_UPDATE_TS[inc_id] = time.time()
    send_whatsapp_text_reply(
        cfg.MANAGER_WHATSAPP_NUMBER,
        f"📍 *Live Location — {vehicle_id}*\n\n{place_name}\n🗺️ https://www.google.com/maps?q={lat},{lng}"
    )
    send_whatsapp_text_reply(sender.raw, "✅ Location manager ko forward kar diya gaya.")
    return {"status": "success", "action": "Driver location forwarded to manager."}
