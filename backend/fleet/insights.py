"""Keyword extraction, per-vehicle repeat-issue detection and learned knowledge-base docs."""
import hashlib
import re
import time
from typing import Any, Dict, List, Optional

from fleet import state
from fleet.config import cfg


def extract_keywords(text: str, limit: int = 10) -> List[str]:
    """Chhote, significant keywords (3+ letters, common stopwords hataakar)
    nikaalta hai — self-learning knowledge base aur per-vehicle predictive
    pattern detection, dono isi shared logic ko use karte hain taaki 'same
    issue' ki matching ek hi jagah se define ho."""
    raw_words = re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
    stopwords = {"the", "and", "for", "with", "not", "has", "have", "was", "are"}
    keywords = list(dict.fromkeys(w for w in raw_words if w not in stopwords))[:limit]
    if not keywords:
        keywords = [(text or "").lower()[:30]]
    return keywords


def record_and_check_vehicle_pattern(vehicle_id: Optional[str], issue_type: str) -> Optional[str]:
    """Feature: Per-Vehicle Predictive Maintenance. Har naye incident ko us
    vehicle ki (SQLite-persisted) history me record karta hai, aur agar isi
    tarah ka issue (overlapping keywords) is vehicle par pichle
    PREDICTIVE_PATTERN_WINDOW_DAYS din me kam-se-kam PREDICTIVE_PATTERN_MIN_REPEATS
    baar ho chuka hai, to manager ke liye ek chhota predictive-maintenance
    warning string return karta hai — taaki sirf part badalna hi solution na
    ban jaaye, ek baar poora inspection bhi ho jaaye. Returns None agar koi
    repeat-pattern nahi mila (ya vehicle_id hi nahi hai)."""
    if not vehicle_id:
        return None

    new_keywords = set(extract_keywords(issue_type))
    now = time.time()
    cutoff = now - (cfg.PREDICTIVE_PATTERN_WINDOW_DAYS * 86400)

    history = state.VEHICLE_ISSUE_HISTORY.get(vehicle_id, [])
    recent_history = [h for h in history if h.get("ts", 0) >= cutoff]

    matches = [h for h in recent_history if new_keywords & set(h.get("keywords", []))]

    recent_history.append({"issue_type": issue_type, "keywords": list(new_keywords), "ts": now})
    state.VEHICLE_ISSUE_HISTORY[vehicle_id] = recent_history

    if len(matches) >= cfg.PREDICTIVE_PATTERN_MIN_REPEATS:
        return (
            f"🔁 *Predictive Maintenance Alert:* {vehicle_id} par milta-julta issue pichle "
            f"{cfg.PREDICTIVE_PATTERN_WINDOW_DAYS} din me {len(matches)} baar aa chuka hai (is baar "
            f"milaakar {len(matches) + 1}). Sirf part replace karne ke bajaye ek poora inspection "
            f"karwana recommend hai."
        )
    return None


def build_learned_doc_from_incident(incident_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Manager-approved AI diagnosis ko ek proper knowledge-base document me
    convert karta hai — agli baar wahi/similar issue aane par ye manual-grounded
    high-confidence match banega, Gemini fallback nahi."""
    issue_type = incident_ctx.get("issue_type", "")
    keywords = extract_keywords(issue_type)

    # md5 only derives a stable id here (not a security use) — same ids as before.
    doc_id = "AUTOLEARN-" + hashlib.md5(issue_type.encode("utf-8"), usedforsecurity=False).hexdigest()[:8].upper()
    return {
        "id": doc_id,
        "manual": "AI-Learned Diagnosis (Manager-Verified)",
        "keywords": keywords,
        "part_code": incident_ctx.get("recommended_part", "Standard Certified Heavy Fleet Repair Kit (Part #FL-GEN-01)"),
        "content": incident_ctx.get("diagnostic_summary", issue_type)
    }
