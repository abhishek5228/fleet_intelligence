"""Everything that talks to Google Gemini, plus the keyword fallback used when it is down."""
import base64
import json
import re
from typing import Any, Dict, Optional

import requests

from fleet.config import cfg

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _generate(body: Dict[str, Any], *, timeout: int, model: str = "gemini-flash-latest") -> str:
    """POST `body` to Gemini and return the first candidate's text. Raises on HTTP/shape errors.

    The key goes in a header, never the URL, so it cannot leak into an exception message.
    """
    resp = requests.post(GEMINI_URL.format(model=model), json=body,
                         headers={"x-goog-api-key": cfg.GEMINI_API_KEY}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _parse_json_reply(raw_text: str) -> Dict[str, Any]:
    """Parse a JSON reply that the model may have wrapped in markdown fences."""
    cleaned = raw_text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()
    return json.loads(cleaned)


def transcribe_voice_note(audio_bytes: bytes, mime_type: str = "audio/ogg") -> Optional[Dict[str, str]]:
    """Voice note (Hindi/Hinglish/regional) ko Gemini se seedha samajh ke
    incident ke zaroori fields (vehicle_id, location, issue_type) nikaalta
    hai — driver ko form fill karne ki zaroorat nahi, sirf bol dena kaafi hai."""
    if not cfg.GEMINI_API_KEY:
        return None
    try:
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        prompt = (
            "This is a voice note from a truck/bus driver in India, likely in Hindi, "
            "Hinglish, or a regional language, reporting a vehicle breakdown. Listen to it "
            "and respond ONLY with strict JSON, no markdown fences, no preamble, in this "
            "exact shape:\n"
            '{"vehicle_id": "<vehicle number/ID if mentioned, else \\"UNKNOWN\\">", '
            '"location": "<place name if mentioned, else \\"UNKNOWN\\">", '
            '"issue_type": "<short English description of the mechanical issue described>"}'
        )
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": audio_b64}}
                ]
            }]
        }
        parsed = _parse_json_reply(_generate(body, timeout=20))
        if parsed.get("issue_type"):
            return {
                "vehicle_id": parsed.get("vehicle_id", "UNKNOWN"),
                "location": parsed.get("location", "UNKNOWN"),
                "issue_type": parsed["issue_type"],
            }
    except Exception as e:
        print(f"DEBUG VOICE NOTE TRANSCRIBE ERROR: {str(e)}")
    return None


# ==========================================
# Features 1 & 2: IN-CHAT PHOTO DIAGNOSIS + SELF-SERVICE REPAIR GUIDANCE —
# driver jab bhi WhatsApp chat me kabhi bhi (sirf initial report ke time
# nahi) part/engine/tyre/dashboard ki photo bheje, Gemini Vision use turant
# analyze karke severity aur ek verdict deta hai: agar chhota/safe issue hai
# to driver ko khud-thik-karne ke step-by-step instructions milte hain
# (mechanic dispatch ki zaroorat nahi), warna manager ko photo + AI
# diagnosis ke saath escalate kar diya jaata hai.
# ==========================================
def diagnose_incident_photo(image_bytes: bytes, mime_type: str, incident_ctx: dict) -> Optional[Dict[str, Any]]:
    """Gemini Vision se photo ka real diagnosis leta hai. Return shape:
    {visible_damage, severity, can_self_fix, diy_steps, recommended_part,
    driver_message} — ya None agar Gemini unavailable/fail ho jaaye (caller
    tab safe generic fallback message dikhata hai, kabhi crash nahi hota)."""
    if not cfg.GEMINI_API_KEY:
        return None
    try:
        img_b64 = base64.b64encode(image_bytes).decode("utf-8")
        prompt = (
            "You are a heavy commercial vehicle (truck/bus) diagnostic assistant for an Indian fleet "
            "operator. A driver just sent this photo in WhatsApp chat about their vehicle "
            f"(Vehicle ID: {incident_ctx.get('vehicle_id', 'unknown')}, previously reported issue: "
            f"'{incident_ctx.get('issue_type', 'not specified')}'). Look at the photo carefully and give "
            "a real, specific assessment — never a generic placeholder.\n\n"
            "Respond ONLY with strict JSON, no markdown fences, no preamble, in this exact shape:\n"
            '{"visible_damage": "<specific description of what you actually see>", '
            '"severity": "LOW"|"MODERATE"|"HIGH"|"CRITICAL", '
            '"can_self_fix": true/false, '
            '"diy_steps": ["step 1", "step 2", ...] (empty list if can_self_fix is false), '
            '"recommended_part": "<part/kit name if a replacement looks needed, else empty string>", '
            '"driver_message": "<a short 2-4 sentence WhatsApp reply in Hinglish (Hindi written in roman '
            'letters, natural and driver-friendly) explaining the diagnosis, and either the DIY steps or '
            'that help/a mechanic is being arranged>"}\n\n'
            "Rules: only set can_self_fix=true for genuinely minor, safe roadside fixes needing no tools/"
            "training (e.g. a loose connector, topping up coolant/water, a stuck relay, a blown fuse, "
            "reseating a battery terminal). NEVER suggest self-fixing brakes, steering, the fuel system, "
            "anything electrical under load, or anything requiring lifting the vehicle or working near a "
            "running engine or hot parts — for those, can_self_fix must be false. If the photo is unclear "
            "or you are not confident, set can_self_fix=false, severity to MODERATE, and ask for a "
            "clearer/well-lit photo in driver_message instead of guessing."
        )
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": img_b64}}
                ]
            }],
            "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"}
        }
        parsed = _parse_json_reply(_generate(body, timeout=20))
        if parsed.get("driver_message"):
            parsed.setdefault("severity", "MODERATE")
            if parsed["severity"] not in ("LOW", "MODERATE", "HIGH", "CRITICAL"):
                parsed["severity"] = "MODERATE"
            parsed.setdefault("can_self_fix", False)
            parsed.setdefault("diy_steps", [])
            parsed.setdefault("recommended_part", "")
            parsed.setdefault("visible_damage", "Not clearly identifiable from photo")
            return parsed
    except Exception as e:
        print(f"DEBUG PHOTO DIAGNOSIS ERROR: {str(e)}")
    return None


def generate_ai_diagnosis(issue_type: str) -> Optional[Dict[str, str]]:
    """Jab koi bhi hardcoded manual/keyword se match nahi hota, Gemini se
    best-effort real diagnosis generate karta hai — taaki RAG sirf fixed
    knowledge-base categories tak limited na rahe aur kisi bhi real-world
    breakdown query ka genuine jawab de sake. Result hamesha caller ke
    through 'AI-generated, verify manually' ke taur pe flag hota hai —
    kabhi bhi certified-manual jitna confident nahi dikhaya jaata."""
    if not cfg.GEMINI_API_KEY:
        return None
    try:
        prompt = (
            "You are a heavy commercial vehicle (truck/bus) diagnostic assistant "
            "for an Indian fleet operator. A driver reported this breakdown issue:\n\n"
            f"\"{issue_type}\"\n\n"
            "Respond ONLY with strict JSON, no markdown fences, no preamble, in this "
            "exact shape:\n"
            '{"diagnostic_summary": "<one sentence likely cause>", '
            '"recommended_part": "<short generic part or kit name>"}'
        )
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        parsed = _parse_json_reply(_generate(body, timeout=10))
        if parsed.get("diagnostic_summary") and parsed.get("recommended_part"):
            return {
                "diagnostic_summary": parsed["diagnostic_summary"],
                "recommended_part": parsed["recommended_part"],
            }
    except Exception as e:
        print(f"DEBUG AI DIAGNOSIS FALLBACK ERROR: {str(e)}")
    return None


def call_ai_agent(manager_text: str, incident_ctx: dict) -> dict:
    """
    Har ek incoming message ko Gemini AI Agent ke paas bhejta hai,
    uska smart decision nikalta hai aur professional WhatsApp reply banata hai.
    """
    if not cfg.GEMINI_API_KEY:
        return keyword_fallback(manager_text)

    system_prompt = (
        "You are an advanced Autonomous Enterprise Fleet Operations AI Agent for Beekay Infra & Logistics. "
        "A manager or driver has sent a WhatsApp message about a vehicle breakdown incident. "
        "First, understand what they are ACTUALLY asking or instructing — do not give a generic "
        "'approved and dispatched' reply unless they are actually approving something. "
        "If they ask for a phone number, contact, or showroom number — give the exact hub_phone value "
        "provided in the context, do NOT say you'll 'share it shortly' if the number is already given to you. "
        "If they ask a factual question, answer it directly and specifically using the context given. "
        "If they give an approval/rejection/reroute instruction, classify it accordingly. "
        "If the DRIVER is sharing a status/location/progress update (e.g. 'leaving for the hub now', "
        "'reached the workshop', 'on the way', 'running late') and asking or implying that the manager "
        "should be told — classify this as DRIVER_STATUS_UPDATE. In reply_text for this case, "
        "confirm to the driver that you are passing it on RIGHT NOW ('Noted, informing your manager now') — "
        "never claim the manager has ALREADY been informed, since that message is sent separately, after "
        "your reply, by the system itself. ALSO add a short nudge asking the driver to also share their "
        "live WhatsApp location (e.g. 'Kripya apna live location bhi share kar dein 📍'), since WhatsApp "
        "only lets the driver share their own location by tapping the attachment/location option — the "
        "system cannot pull or request it automatically.\n"
        "Classify their decision into exactly one of: APPROVED_AND_DISPATCHED, APPROVED_LOCAL_MECHANIC_REROUTED, "
        "REJECTED_REROUTING, INFO_REQUEST_ANSWERED (for questions/requests for info like phone numbers), "
        "DRIVER_STATUS_UPDATE (driver's location/progress update meant for the manager), "
        "or CUSTOM_INSTRUCTION_LOGGED (for anything else specific).\n\n"
        "LANGUAGE MATCHING (important): always write reply_text in the SAME language and script the sender "
        "just used — if they wrote in Hindi/Devanagari script, reply in Devanagari; if they wrote Hinglish "
        "(Hindi words in Roman/English letters), reply in Hinglish the same way; if they wrote in plain "
        "English, reply in English. Never switch script or language on your own, and never mix scripts.\n\n"
        "URGENCY ASSESSMENT (important): also assess how urgent their message is and set 'urgency' to exactly "
        "one of: CRITICAL (life-threatening — accident, fire, vehicle overturned, someone injured/hurt), "
        "URGENT (serious safety/operational risk needing the manager's immediate attention right now — brake "
        "or steering failure, major fuel/oil/gas leak, breakdown on a highway at night, cargo at risk), or "
        "NORMAL (routine status updates, questions, approvals, minor issues). Default to NORMAL unless the "
        "message clearly indicates otherwise — never inflate routine messages to sound more serious than they are.\n\n"
        "Write a short (1-2 sentence), specific, professional WhatsApp reply that directly addresses what "
        "they asked or said — using the real details from the context (hub name, phone number, part name) "
        "wherever relevant.\n\n"
        "Respond ONLY as valid JSON in this exact format without markdown code blocks: "
        "{\"decision\": \"...\", \"reply_text\": \"...\", \"urgency\": \"NORMAL\"|\"URGENT\"|\"CRITICAL\"}"
    )

    user_prompt = (
        f"Incident Context:\n"
        f"- Vehicle ID: {incident_ctx.get('vehicle_id')}\n"
        f"- Issue Type: {incident_ctx.get('issue_type')}\n"
        f"- Assigned Hub: {incident_ctx.get('hub')}\n"
        f"- Hub Phone Number: {incident_ctx.get('hub_phone')}\n"
        f"- RAG Suggested Part: {incident_ctx.get('recommended_part')}\n\n"
        f"Incoming Message: \"{manager_text}\""
    )

    for attempt_model in ["gemini-flash-latest", "gemini-flash-lite-latest"]:
        try:
            url = GEMINI_URL.format(model=attempt_model)
            headers = {
                "x-goog-api-key": cfg.GEMINI_API_KEY,
                "Content-Type": "application/json"
            }
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {"temperature": 0.3, "response_mime_type": "application/json"}
            }
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            if res.status_code == 503:
                continue

            res.raise_for_status()
            data = res.json()
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            cleaned_content = content.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(cleaned_content)

            if parsed.get("decision") and parsed.get("reply_text"):
                parsed.setdefault("urgency", "NORMAL")
                if parsed["urgency"] not in ("NORMAL", "URGENT", "CRITICAL"):
                    parsed["urgency"] = "NORMAL"
                return parsed
        except Exception as e:
            print(f"DEBUG GEMINI AI ERROR [{attempt_model}]: {str(e)}")
            continue

    return keyword_fallback(manager_text)


# Feature 4 (Urgency Detection) safety-net for when Gemini is unavailable —
# no real language-understanding is possible here, so this is a simple but
# high-recall keyword net for genuinely life-threatening situations only.
_CRITICAL_URGENCY_KEYWORDS = [
    "accident", "durghatna", "fire", "aag lag", "aag laga", "blast", "vishfot",
    "injured", "injury", "chot lag", "ghayal", "palat gaya", "palti", "overturn",
    "jal gaya", "jal raha", "burn", "khoon"
]


def keyword_fallback(text_body: str) -> dict:
    t = text_body.lower()
    urgency = "CRITICAL" if any(kw in t for kw in _CRITICAL_URGENCY_KEYWORDS) else "NORMAL"

    if "local" in t or "fatuha" in t or "sasta" in t or "bypass" in t:
        result = {
            "decision": "APPROVED_LOCAL_MECHANIC_REROUTED",
            "reply_text": "✅ Understood — the vehicle has been rerouted to the local mechanic."
        }
    elif re.search(r"\b(ok|okay|haan|kardo|approve[d]?)\b", t):  # whole words: "broken" must not match "ok"
        result = {
            "decision": "APPROVED_AND_DISPATCHED",
            "reply_text": "✅ Repair approved, dispatch sequence has been initiated."
        }
    elif "cancel" in t or "reject" in t or re.search(r"\bmat\b", t):  # "mat" = "don't" in Hindi, not "material"
        result = {
            "decision": "REJECTED_REROUTING",
            "reply_text": "❌ Request rejected, vehicle rerouting initiated."
        }
    elif any(kw in t for kw in [
        "leaving for", "on my way", "on the way", "reached", "arrived",
        "nikal", "ja rha", "ja raha", "pahunch", "running late", "delay ho"
    ]):
        result = {
            "decision": "DRIVER_STATUS_UPDATE",
            "reply_text": "Noted, informing your manager now. Kripya apna live location bhi share kar dein 📍"
        }
    else:
        result = {
            "decision": f"CUSTOM_INSTRUCTION_LOGGED: {text_body}",
            "reply_text": "📝 Your instruction has been recorded, the team will follow up."
        }

    if urgency == "CRITICAL":
        result["reply_text"] = "🆘 Samajh gaya, ye emergency hai — manager ko turant alert kiya jaa raha hai. " + result["reply_text"]
    result["urgency"] = urgency
    return result
