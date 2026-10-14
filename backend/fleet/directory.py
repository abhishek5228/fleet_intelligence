"""Lookups for who is who: driver phones by vehicle, and service-hub phone numbers."""
from typing import Optional

import requests

from fleet.config import cfg


def lookup_driver_phone(vehicle_id: Optional[str]) -> Optional[str]:
    """DRIVER_WHATSAPP_MAPPING se phone dhoondta hai — case/whitespace ka
    farak (jaise 'br01gp9621 ' vs 'BR01GP9621') ab match fail nahi karega.
    Agar phir bhi na mile, Render logs me exact vehicle_id print ho jaata
    hai taaki mapping me kya add/fix karna hai turant pata chal jaaye."""
    if not vehicle_id:
        print("DEBUG DRIVER LOOKUP: vehicle_id khud hi empty/None hai — incident_ctx me save nahi hua tha.")
        return None
    normalized = vehicle_id.strip().upper()
    for vid, phone in cfg.DRIVER_WHATSAPP_MAPPING.items():
        if vid.strip().upper() == normalized:
            return phone
    print(f"DEBUG DRIVER LOOKUP FAILED: vehicle_id={vehicle_id!r} DRIVER_WHATSAPP_MAPPING me kisi key se match nahi hua. "
          f"Available keys: {list(cfg.DRIVER_WHATSAPP_MAPPING.keys())}")
    return None


def resolve_hub_phone(hub_record: dict, hub_index: int = 0) -> str:
    """Static phone (fallback hubs) ya Google Place Details se phone nikaalta hai.

    TEMPORARY (testing phase): asli hub numbers abhi wire-up nahi hain, isliye
    TEST_HUB_NUMBERS list se hub_index % len() ke hisaab se cyclically ek
    number assign hota hai (hub 1 → pehla number, hub 2 → doosra, hub 3 →
    phir pehla, waghera). Jaise hi real hub phone numbers ready ho jaayein,
    TEST_HUB_NUMBERS ko [] kar dena — neeche wala Google-Place-Details/
    fallback logic apne aap use hone lagega."""
    if cfg.TEST_HUB_NUMBERS:
        return cfg.TEST_HUB_NUMBERS[hub_index % len(cfg.TEST_HUB_NUMBERS)]
    if hub_record.get("phone"):
        return hub_record["phone"]
    if hub_record.get("place_id") and cfg.GOOGLE_MAPS_API_KEY:
        try:
            details_url = "https://maps.googleapis.com/maps/api/place/details/json"
            details_params = {
                "place_id": hub_record["place_id"],
                "fields": "formatted_phone_number,international_phone_number",
                "key": cfg.GOOGLE_MAPS_API_KEY
            }
            d_data = requests.get(details_url, params=details_params, timeout=5).json()
            phone = d_data.get("result", {}).get("formatted_phone_number") or d_data.get("result", {}).get("international_phone_number")
            if phone:
                return phone
        except Exception:
            pass
    return "Contact number not available — team will call and share shortly"
