"""Spare-part price comparison: ask vendors, collect quotes, negotiate once, notify the manager."""
import re
import threading
from typing import Any, Dict, List

from fleet import state
from fleet.config import cfg
from fleet.directory import resolve_hub_phone
from fleet.phones import last10
from fleet.whatsapp import (
    send_vendor_availability_buttons,
    send_vendor_choice_buttons,
    send_whatsapp_text_reply,
)


# ==========================================
# SPARE-PART PRICE COMPARISON — jab service centre milta hai, top nearby
# vendors se real-time quote maanga jaata hai; jawab aane par AI compare
# karke best price manager ko dikhata hai, aur zaroorat par ek round
# negotiate bhi kar leta hai.
# ==========================================
@state.locked
def request_spare_part_quotes(incident_id: str, hub_records: List[Dict[str, Any]], part_name: str):
    """Top 3 nearby hubs ko WhatsApp par part ki price/availability poochta hai.
    Agar TEST_VENDOR_NUMBERS me kuch bhara hai, wahi use hoga (testing mode) —
    warna real Google-Places-derived nearby vendors automatically use hote hain."""
    if cfg.TEST_VENDOR_NUMBERS:
        targets = list(cfg.TEST_VENDOR_NUMBERS)
    else:
        targets = []
        for idx, hub in enumerate(hub_records[:3]):
            phone = resolve_hub_phone(hub, idx)
            if phone and "not available" not in phone.lower():
                targets.append({"name": hub["name"], "phone": phone})

    if not targets:
        return

    state.PENDING_QUOTES[incident_id] = {
        "part_name": part_name,
        "vendors": {
            last10(t["phone"]): {
                "hub_name": t["name"], "phone": t["phone"],
                "status": "waiting", "price": None, "negotiated": False,
                "awaiting_cost": False
            }
            for t in targets
        }
    }

    failed_vendors = []
    for t in targets:
        vendor_key = last10(t["phone"])
        result = send_vendor_availability_buttons(t["phone"], incident_id, vendor_key, part_name)
        delivery_failed = (
            result.get("status") == "error"
            or result.get("delivery_failed") is True
        )
        if delivery_failed:
            failed_vendors.append(t["name"])
            quote_ctx = state.PENDING_QUOTES[incident_id]
            quote_ctx["vendors"].pop(vendor_key, None)
            state.PENDING_QUOTES[incident_id] = quote_ctx

    if failed_vendors:
        send_whatsapp_text_reply(
            cfg.MANAGER_WHATSAPP_NUMBER,
            f"⚠️ Could not reach {len(failed_vendors)} vendor(s) for a price quote on "
            f"'{part_name}' — their number may not be reachable via WhatsApp Business API "
            f"yet (needs a prior conversation or an approved message template): "
            f"{', '.join(failed_vendors)}"
        )

    if not state.PENDING_QUOTES.get(incident_id, {}).get("vendors"):
        state.PENDING_QUOTES.pop(incident_id, None)
        return

    # Safety-net: manager gets whatever quotes are ready 40s after the
    # enquiry goes out, even if not every vendor has replied yet — so
    # "one vendor added, one vendor answered" cases never sit unnotified.
    timer = threading.Timer(40.0, finalize_quotes_after_timeout, args=[incident_id])
    timer.daemon = True  # never keep the process alive just to run a reminder
    timer.start()


@state.locked
def notify_manager_with_quotes(incident_id: str) -> bool:
    """Ready ho chuke vendor quotes manager ko choose-buttons ke roop me
    bhejta hai. True return karta hai sirf jab kuch actually bheja gaya ho —
    is se all_done-trigger aur 40s-timeout dono isi ek jagah se manager ko
    notify karte hain, duplicate logic nahi."""
    quote_ctx = state.PENDING_QUOTES.get(incident_id)
    if not quote_ctx or quote_ctx.get("manager_notified"):
        return False
    quoted = [
        {**v, "vendor_key": k} for k, v in quote_ctx["vendors"].items()
        if v["status"] == "quoted" and v.get("price")
    ]
    if not quoted:
        return False
    quoted.sort(key=lambda v: v["price"])
    result = send_vendor_choice_buttons(incident_id, quoted, quote_ctx["part_name"])
    # NOTE: pehle sirf "delivery_failed" flag check hota tha — agar
    # send_vendor_choice_buttons ke andar exception (network/timeout) aata,
    # wo {"status": "error", ...} return karta, jisme "delivery_failed" key
    # hoti hi nahi — isliye fallback text bhi silently skip ho jaata tha aur
    # manager ko interactive buttons YA fallback, dono me se kuch nahi
    # jaata tha (jabki manager_notified=True set ho jaata, dobara try bhi
    # nahi hota). Ab dono cases (non-200 response AND exception) fallback
    # trigger karte hain — same fix jo ACCEPTREPAIR button flow me hai.
    send_failed = isinstance(result, dict) and (
        result.get("delivery_failed") or result.get("status") == "error"
    )
    if send_failed:
        # Interactive buttons na jaa paaye (button-title limit, template
        # issue, network error, etc.) — manager ko kam se kam plain text me
        # list to milni chahiye, taaki wo manually reply karke vendor
        # choose kar sake.
        fallback_lines = [f"💰 *Spare Part Price Comparison*\n*Part:* {quote_ctx['part_name']}\n"]
        for i, v in enumerate(quoted[:3]):
            tag = " ✅ Best" if i == 0 else ""
            fallback_lines.append(f"{i+1}. {v['hub_name']}: ₹{v['price']:.0f}{tag}")
        fallback_lines.append("\n⚠️ Interactive buttons deliver nahi ho paaye — reply karke bataayein kaunsa vendor (1/2/3) choose karna hai.")
        send_whatsapp_text_reply(cfg.MANAGER_WHATSAPP_NUMBER, "\n".join(fallback_lines))
    quote_ctx["manager_notified"] = True
    state.PENDING_QUOTES[incident_id] = quote_ctx
    return True


@state.locked
def finalize_quotes_after_timeout(incident_id: str):
    """40 second ke baad chalta hai (background thread se) — jo bhi quotes
    us waqt tak ready hain, unhe manager ko bhej deta hai, baaki vendors
    baad me reply karein to woh apne aap all_done path se handle ho jaayega."""
    try:
        notify_manager_with_quotes(incident_id)
    except Exception as e:
        print(f"DEBUG QUOTE TIMEOUT ERROR: {str(e)}")


@state.locked
def handle_vendor_quote_reply(incident_id: str, vendor_10_digit: str, text_body: str) -> bool:
    """Vendor Yes button dabane ke baad price ka number bhejta hai — ye
    function sirf usi cost-reply ko handle karta hai (regex se digit nikaal
    ke), Gemini par depend nahi karta, isliye availability-parsing wali
    purani ambiguity/silent-failure ab possible nahi hai. False return
    karta hai agar vendor ne abhi Yes/No button hi nahi dabaya (taaki caller
    is text ko normal driver-chat se confuse na kare)."""
    quote_ctx = state.PENDING_QUOTES.get(incident_id)
    if not quote_ctx or vendor_10_digit not in quote_ctx["vendors"]:
        return False

    vendor = quote_ctx["vendors"][vendor_10_digit]

    if not vendor.get("awaiting_cost"):
        # Vendor ne Yes/No button nahi dabaya — is text ko ignore karo.
        return False

    digits = re.findall(r'\d[\d,]*', text_body)
    if not digits:
        send_whatsapp_text_reply(vendor["phone"], "Kripya sirf price number bhejein (jaise: 10000).")
        return True

    price = float(max(digits, key=lambda d: len(d.replace(",", ""))).replace(",", ""))

    # Ek round negotiation: agar koi doosra vendor sasta quote de chuka hai
    # aur is vendor se abhi negotiate nahi kiya, to AI khud ek counter-offer
    # bhej deta hai — manager ko intervene nahi karna padta. Kisi bhi vendor
    # ka EXACT price doosre vendor ko kabhi disclose nahi karte — sirf itna
    # bataate hain ki "better rate mil sakta hai", competitor ka number nahi.
    cheaper_others = [
        v["price"] for k, v in quote_ctx["vendors"].items()
        if k != vendor_10_digit and v["price"] is not None
    ]
    if cheaper_others and min(cheaper_others) < price and not vendor.get("negotiated"):
        vendor["negotiated"] = True
        vendor["awaiting_cost"] = True
        send_whatsapp_text_reply(
            vendor["phone"],
            "Dhanyavaad. Hume isse better rate mil sakta hai. "
            "Kya aap apna price thoda kam kar sakte hain? Kripya number bhejein."
        )
        vendor["status"] = "negotiating"
        vendor["price"] = price  # provisional, may update on their next reply
    else:
        vendor["price"] = price
        vendor["status"] = "quoted"
        vendor["awaiting_cost"] = False

    # Explicit re-save: with a persistent (SQLite-backed) store, .get() returns
    # a deserialized copy, not a live reference — mutations above won't stick
    # unless we write the whole context back, including for partial progress
    # (not all vendors replied yet).
    state.PENDING_QUOTES[incident_id] = quote_ctx

    # Jab sab vendors se final response aa jaye (quoted/unavailable), manager
    # ko choose-karne-wale buttons abhi hi bhejo (40s timer ka wait nahi
    # karna padega) — is se single-vendor case turant handle ho jaata hai.
    all_done = all(v["status"] in ("quoted", "unavailable") for v in quote_ctx["vendors"].values())
    if all_done and not quote_ctx.get("manager_notified"):
        if not notify_manager_with_quotes(incident_id):
            send_whatsapp_text_reply(
                cfg.MANAGER_WHATSAPP_NUMBER,
                f"⚠️ None of the {len(quote_ctx['vendors'])} nearby vendors had "
                f"'{quote_ctx['part_name']}' available."
            )
            del state.PENDING_QUOTES[incident_id]

    return True
