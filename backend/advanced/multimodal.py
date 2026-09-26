"""
multimodal.py
===============
NEW, ADDITIVE MODULE — does not modify any existing file.

Feature 5: Multi-Modal Inputs (driver sends a photo of the damaged part on
WhatsApp instead of / in addition to typing text).

Flow:
    1. WhatsApp webhook receives an image message (media id).
    2. download_whatsapp_media() fetches the image bytes using the same
       WHATSAPP_TOKEN pattern your main.py already uses.
    3. identify_damaged_part() sends the image to Gemini Vision, asking it
       to describe the visible damage in a structured way.
    4. That structured description becomes the query into your
       HybridSearchEngine (hybrid_search.py) to find the matching part number.

This keeps the vision call and the RAG call clearly separated, so the
vision model NEVER invents a part number itself — it only describes what it
sees, and the hallucination-guarded RAG pipeline (self_rag.py) does the
actual part-number lookup grounded in your real documents.
"""

import os
import base64
import json
from typing import Dict, Any, Optional

import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.0-flash")
GEMINI_VISION_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_VISION_MODEL}:generateContent"
)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")


def download_whatsapp_media(media_id: str) -> Optional[bytes]:
    """Mirrors the two-step Meta Graph API media download flow:
    1. GET /{media_id} -> returns a temporary media URL
    2. GET that URL with the same auth header -> raw bytes
    """
    if not WHATSAPP_TOKEN or WHATSAPP_TOKEN == "YOUR_TOKEN":
        print(f"[multimodal] SIMULATED media download for media_id={media_id}")
        return None

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        meta_resp = requests.get(
            f"https://graph.facebook.com/v26.0/{media_id}", headers=headers, timeout=10
        )
        meta_resp.raise_for_status()
        media_url = meta_resp.json()["url"]

        media_resp = requests.get(media_url, headers=headers, timeout=15)
        media_resp.raise_for_status()
        return media_resp.content
    except Exception as e:
        print(f"[multimodal] WARNING: media download failed ({e})")
        return None


def identify_damaged_part(
    image_bytes: bytes, vehicle_context: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sends the image to Gemini Vision and asks for a STRUCTURED description
    only (never a part number — that stays grounded in your RAG documents).

    Returns:
        {
          "component_area": "e.g. engine oil filter housing",
          "visible_damage": "e.g. cracked housing with oil seepage",
          "confidence": "high"/"medium"/"low",
          "suggested_search_query": "<text to feed into hybrid_search>"
        }
    """
    if not GEMINI_API_KEY:
        return {"error": "GEMINI_API_KEY not set", "suggested_search_query": None}

    img_b64 = base64.b64encode(image_bytes).decode("utf-8")

    system_prompt = (
        "You are a heavy-truck mechanical inspection assistant. Look at the "
        "photo of a truck part/component sent by a driver. Describe ONLY what "
        "you visually observe (location on the vehicle, type of damage, "
        "material, any visible markings/text). "
        "Do NOT guess a specific part number or SKU — only describe what you see. "
        "Respond ONLY as JSON: "
        '{"component_area": "...", "visible_damage": "...", '
        '"visible_markings_text": "...", "confidence": "high|medium|low"}'
    )
    user_text = "Analyze this truck component photo for damage."
    if vehicle_context:
        user_text += f" Vehicle context: {vehicle_context}"

    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": user_text},
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                ],
            }
        ],
        "generationConfig": {"response_mime_type": "application/json"},
    }

    try:
        resp = requests.post(
            GEMINI_VISION_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
    except Exception as e:
        print(f"[multimodal] WARNING: vision call failed ({e})")
        return {"error": str(e), "suggested_search_query": None}

    # Build a search query for HybridSearchEngine from the structured vision output
    query_parts = [
        parsed.get("component_area", ""),
        parsed.get("visible_damage", ""),
        parsed.get("visible_markings_text", ""),
    ]
    parsed["suggested_search_query"] = " ".join(p for p in query_parts if p).strip()
    return parsed


def process_whatsapp_image_message(media_id: str, vehicle_id: Optional[str] = None) -> Dict[str, Any]:
    """End-to-end helper: WhatsApp media_id -> structured damage description
    -> ready-to-use search query for hybrid_search.HybridSearchEngine.search().
    """
    image_bytes = download_whatsapp_media(media_id)
    if image_bytes is None:
        return {"error": "Could not download image", "suggested_search_query": None}

    context = f"Vehicle ID: {vehicle_id}" if vehicle_id else None
    return identify_damaged_part(image_bytes, vehicle_context=context)
