"""Outbound WhatsApp Business Cloud API calls (and media download).

Without WHATSAPP_TOKEN every sender runs in a safe simulated mode (it logs and returns a
"simulated" result), so the app never crashes on a fresh checkout.
"""
import re
from typing import Any, Dict, List, Optional

import requests

from fleet.config import cfg
from fleet.phones import digits

GRAPH_MESSAGES_URL = "https://graph.facebook.com/v26.0/{phone_id}/messages"


def _live() -> bool:
    """True when real sends are configured; otherwise senders simulate."""
    return bool(cfg.WHATSAPP_TOKEN) and cfg.WHATSAPP_TOKEN != "YOUR_TOKEN"


def _messages_url() -> str:
    return GRAPH_MESSAGES_URL.format(phone_id=cfg.WHATSAPP_PHONE_ID)


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {cfg.WHATSAPP_TOKEN}", "Content-Type": "application/json"}


def _post_interactive(to_digits: str, interactive: Dict[str, Any], label: str) -> Dict[str, Any]:
    """POST one interactive (button) message.

    Returns {"status_code", "delivery_failed"}, or {"status": "error", "details"} if the
    request itself failed. Callers use either signal to fall back to plain text.
    Success logs only the status code: Meta's success body echoes the recipient's number.
    """
    body = {"messaging_product": "whatsapp", "to": to_digits, "type": "interactive", "interactive": interactive}
    try:
        res = requests.post(_messages_url(), json=body, headers=_headers(), timeout=10)
        if res.status_code == 200:
            print(f"DEBUG {label} RESPONSE [200]")
        else:
            print(f"DEBUG {label} RESPONSE [{res.status_code}]:", res.text)
            print(f"⚠️ Meta API Error: Could not deliver {label.lower()}.")
        return {"status_code": res.status_code, "delivery_failed": res.status_code != 200}
    except Exception as e:
        print(f"DEBUG {label} EXCEPTION: {str(e)}")
        return {"status": "error", "details": str(e)}


def _json_or_text(res: "requests.Response") -> Any:
    try:
        return res.json()
    except ValueError:
        return {"raw": res.text[:500]}


# ==========================================
# VOICE NOTE SUPPORT — driver bolke breakdown report kar sake, type kiye
# bina. WhatsApp audio message ko Meta se download karke Gemini se transcribe
# + structured fields (vehicle_id, location, issue_type) extract karta hai.
# ==========================================
def download_media(media_id: str) -> Optional[bytes]:
    """Meta Graph API se media ID ka actual audio file download karta hai."""
    if not cfg.WHATSAPP_TOKEN:
        return None
    try:
        meta_url = f"https://graph.facebook.com/v26.0/{media_id}"
        headers = {"Authorization": f"Bearer {cfg.WHATSAPP_TOKEN}"}
        meta_resp = requests.get(meta_url, headers=headers, timeout=10)
        meta_resp.raise_for_status()
        media_url = meta_resp.json().get("url")
        if not media_url:
            return None
        file_resp = requests.get(media_url, headers=headers, timeout=15)
        file_resp.raise_for_status()
        return file_resp.content
    except Exception as e:
        print(f"DEBUG VOICE NOTE DOWNLOAD ERROR: {str(e)}")
        return None


def forward_image(to_phone: str, media_id: str, caption: str = ""):
    """Driver ki bheji hui photo manager ko bhi forward karta hai, isi
    already-uploaded media_id ke through — koi re-download/re-upload nahi.
    Best-effort hai: agar Meta account setup me ye fail ho (media_id doosre
    recipient ke liye valid na ho), caller isse silently ignore karta hai —
    driver-facing diagnosis reply is se kabhi block nahi hota."""
    if not _live() or not media_id:
        return {"status": "simulated_or_skipped"}
    image_payload = {"id": media_id}
    if caption:
        image_payload["caption"] = caption
    body = {
        "messaging_product": "whatsapp",
        "to": digits(to_phone),
        "type": "image",
        "image": image_payload
    }
    try:
        res = requests.post(_messages_url(), json=body, headers=_headers(), timeout=10)
        return {"status_code": res.status_code, "delivery_failed": res.status_code != 200}
    except Exception as e:
        print(f"DEBUG IMAGE FORWARD EXCEPTION: {str(e)}")
        return {"status": "error", "details": str(e)}


def send_vendor_availability_buttons(vendor_phone: str, incident_id: str, vendor_10_digit: str, part_name: str):
    """Vendor ko free-text ki jagah tap-karke-choose-karne wale Yes/No buttons
    bhejta hai availability ke liye — isse Gemini-based natural-language
    parsing par depend nahi karna padta (jo GEMINI_API_KEY na hone ya
    ambiguous replies ki wajah se silently fail ho sakta tha)."""
    body_text = (
        f"🔧 *Fleet Parts Enquiry*\n\n"
        f"Namaste, kya aapke paas ye part available hai?\n"
        f"*Part:* {part_name}"
    )
    buttons = [
        {"type": "reply", "reply": {"id": f"VENDORAVAIL_YES_{incident_id}_{vendor_10_digit}", "title": "✅ Yes, available"}},
        {"type": "reply", "reply": {"id": f"VENDORAVAIL_NO_{incident_id}_{vendor_10_digit}", "title": "❌ Not available"}}
    ]
    if not _live():
        print(f"[simulated] Vendor availability buttons to {vendor_phone}: {body_text}")
        return {"status": "simulated_reply"}
    interactive = {"type": "button", "body": {"text": body_text}, "action": {"buttons": buttons}}
    return _post_interactive(digits(vendor_phone), interactive, "VENDOR AVAIL BUTTON")


def send_vendor_choice_buttons(incident_id: str, quoted: List[Dict[str, Any]], part_name: str):
    """Manager ko sirf text nahi, balki tap-karke-choose-karne wale buttons
    bhejta hai — jo bhi vendor choose kare, uska price+details driver tak
    automatically forward ho jaata hai."""
    body_lines = [f"💰 *Spare Part Price Comparison*\n*Part:* {part_name}\n"]
    buttons = []
    for i, v in enumerate(quoted[:3]):
        tag = " ✅ Best" if i == 0 else ""
        body_lines.append(f"{i+1}. {v['hub_name']}: ₹{v['price']:.0f}{tag}")
        # NOTE: WhatsApp caps interactive reply-button titles at 20 chars —
        # hub_name isn't included here (it's already shown in body_lines
        # above), so this always stays short and never gets silently
        # rejected by Meta the way "₹1250 - Test Vendor 1" (21 chars) did.
        buttons.append({
            "type": "reply",
            "reply": {
                "id": f"PICKVENDOR_{incident_id}_{v['vendor_key']}",
                "title": f"#{i+1}: ₹{v['price']:.0f}"
            }
        })
    body_lines.append("\nKaunsa vendor choose karna hai?")

    if not _live():
        print(f"[simulated] Vendor choice buttons to manager: {body_lines}")
        return {"status": "simulated_reply"}
    interactive = {"type": "button", "body": {"text": "\n".join(body_lines)}, "action": {"buttons": buttons}}
    return _post_interactive(re.sub(r'\D', '', cfg.MANAGER_WHATSAPP_NUMBER or ""), interactive,
                             "VENDOR CHOICE BUTTONS")


def send_driver_accept_repair_button(driver_phone: str, incident_id: str, body_text: str):
    """Driver ko poora breakdown+repair detail ek interactive 'Accept Repair'
    button ke saath bhejta hai — tap karne par hi driver ka AI agent chat
    officially enable hota hai (INCIDENT_CONTEXTS map hoti hai)."""
    if not _live():
        print(f"[simulated] Driver accept-repair button to {driver_phone}: {body_text}")
        return {"status": "simulated_reply"}
    interactive = {
        "type": "button",
        "body": {"text": body_text},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": f"ACCEPTREPAIR_{incident_id}", "title": "Accept Repair ✅"}}
            ]
        }
    }
    return _post_interactive(digits(driver_phone), interactive, "DRIVER ACCEPT-REPAIR BUTTON")


def send_whatsapp_text_reply(to_phone: str, text: str):
    """Sends a plain text reply back on WhatsApp with automatic fallback simulation."""
    if not _live():
        print(f"SIMULATED WHATSAPP TO [{to_phone}]: {text}")
        return {"status": "simulated_reply", "text": text}

    cleaned_to_phone = digits(to_phone)
    body = {
        "messaging_product": "whatsapp",
        "to": cleaned_to_phone,
        "type": "text",
        "text": {"body": text}
    }
    try:
        res = requests.post(_messages_url(), json=body, headers=_headers(), timeout=10)
        if res.status_code == 200:
            print("DEBUG META WHATSAPP API RESPONSE [200]")
        else:
            print(f"DEBUG META WHATSAPP API RESPONSE [{res.status_code}]:", res.text)
            print(f"⚠️ Meta API Error: Could not deliver WhatsApp message to ...{cleaned_to_phone[-4:]}.")
        return {"status_code": res.status_code, "response": res.json() if res.content else {}}
    except Exception as e:
        print(f"DEBUG META API EXCEPTION: {str(e)}")
        return {"status": "error", "details": str(e)}


def send_whatsapp_interactive_approval(incident_id: str, to_phone: str, vehicle_id: str, hub: str,
                                        location: str, issue_type: str, severity: str,
                                        cargo_type: str, recommended_part: str,
                                        predictive_alert: Optional[str] = None):
    """Approve/Reject interactive button bhejta hai — initial manager alert aur
    har automatic reject-reroute cycle, dono isi ek function se guzarte hain.
    predictive_alert diya ho (Feature 6: Per-Vehicle Predictive Maintenance)
    to wahi body text me manager ko dikha diya jaata hai."""
    cleaned_phone = re.sub(r'\D', '', to_phone or "")
    predictive_block = f"\n{predictive_alert}\n" if predictive_alert else ""

    if not _live():
        return {"status": "simulated_interactive_dispatched", "message": f"Google-level Agentic WhatsApp alert sent to {cleaned_phone}."}

    body = {
        "messaging_product": "whatsapp",
        "to": cleaned_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"🚨 *Breakdown Alert — Action Needed*\n\n"
                    f"🚜 *Vehicle:* {vehicle_id} | ⚡ *Sev:* {severity}\n"
                    f"📦 *Cargo:* {cargo_type}\n"
                    f"📍 *Location:* {location}\n"
                    f"🛠️ *Issue:* {issue_type}\n\n"
                    f"🧠 *Hybrid RAG Part:* _{recommended_part}_\n"
                    f"🏢 *Hub:* {hub}\n"
                    f"{predictive_block}\n"
                    f"Authorize repair or reply with instructions?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"APPROVE_{incident_id}", "title": "Approve Repair ✅"}},
                    {"type": "reply", "reply": {"id": f"REJECT_{incident_id}", "title": "Reject & Reroute ❌"}}
                ]
            }
        }
    }
    try:
        res = requests.post(_messages_url(), json=body, headers=_headers(), timeout=10)
    except requests.RequestException as e:
        return {"status": "meta_api_error", "status_code": None, "error_details": type(e).__name__}
    if res.status_code != 200:
        return {"status": "meta_api_error", "status_code": res.status_code, "error_details": _json_or_text(res)}
    return {"status": "dispatched_via_meta_api", "response": _json_or_text(res)}
