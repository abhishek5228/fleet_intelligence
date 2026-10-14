"""Proactive driver follow-up: check in when a driver goes quiet after a repair is dispatched."""
import threading
import time

from fleet import state
from fleet.config import cfg
from fleet.directory import lookup_driver_phone
from fleet.whatsapp import send_whatsapp_text_reply


# ==========================================
# Feature 5: PROACTIVE DRIVER FOLLOW-UP — jab bhi repair dispatch/accept ho
# jaata hai, ek background timer set ho jaata hai. Agar driver ne itni der
# tak koi naya update nahi bheja, AI khud check-in kar leta hai — driver ka
# passively wait nahi karta. Har real driver message DRIVER_LAST_UPDATE_TS
# refresh karta hai (webhook me), isliye agar driver pehle hi update de
# chuka hai to ye check silently kuch nahi karta.
# ==========================================
def _arm(incident_id: str, since: float):
    """Start a daemon timer that runs the check once the silence window has passed.

    Daemon, so a pending 2-hour reminder never holds up a restart or redeploy."""
    timer = threading.Timer(cfg.FOLLOWUP_CHECK_DELAY_SECONDS, run_followup_check, args=[incident_id, since])
    timer.daemon = True
    timer.start()


@state.locked
def schedule_followup_check(incident_id: str):
    """Ek incident ke liye follow-up timer arm karta hai. Idempotent hona
    caller (ACCEPTREPAIR_/APPROVE_ handlers) ki zimmedari hai — wahaan
    incident_ctx['followup_scheduled'] flag se duplicate scheduling roki
    jaati hai."""
    scheduled_at = time.time()
    state.DRIVER_LAST_UPDATE_TS[incident_id] = scheduled_at
    _arm(incident_id, scheduled_at)
    print(f"DEBUG FOLLOWUP SCHEDULED: incident={incident_id} in {cfg.FOLLOWUP_CHECK_DELAY_SECONDS}s")


@state.locked
def run_followup_check(incident_id: str, scheduled_at: float):
    """Timer fire hone par chalta hai. Agar driver ne is beech me khud
    update de diya (DRIVER_LAST_UPDATE_TS scheduled_at se aage badh gaya),
    to chup-chaap kuch nahi karta. Warna driver ko ek friendly check-in aur
    manager ko ek heads-up bhejta hai, aur agla follow-up phir se chain kar
    deta hai jab tak incident reject/close na ho jaaye."""
    try:
        incident_ctx = state.INCIDENT_DETAILS.get(incident_id)
        if not incident_ctx:
            return
        if state.APPROVAL_STATES.get(incident_id) == "REJECTED_REROUTING":
            return

        last_update = state.DRIVER_LAST_UPDATE_TS.get(incident_id, 0)
        if last_update > scheduled_at:
            # Driver ne is beech me khud update de diya — dobara check-in
            # bhejne ki zaroorat nahi, lekin AGLA follow-up window abhi bhi
            # arm karo taaki silence ka pattern chalte rehte hue bhi pakda jaaye.
            _arm(incident_id, last_update)
            return

        vehicle_id = incident_ctx.get("vehicle_id", "N/A")
        driver_phone = lookup_driver_phone(vehicle_id)
        if driver_phone:
            send_whatsapp_text_reply(
                driver_phone,
                f"👋 Namaste, *{vehicle_id}* ki repair kaisi chal rahi hai? "
                f"Koi update ya madad chahiye ho to yahin bata dein."
            )
        hours = round(cfg.FOLLOWUP_CHECK_DELAY_SECONDS / 3600, 1)
        send_whatsapp_text_reply(
            cfg.MANAGER_WHATSAPP_NUMBER,
            f"⏰ *Follow-up:* {vehicle_id} se {hours} ghante se koi update nahi aaya — "
            f"driver ko proactively check-in bhej diya gaya hai."
        )
        # Chain karo — jab tak incident reject/close na ho, silence continue
        # hone par baar-baar follow-up hota rahega, ek-time reminder nahi.
        _arm(incident_id, time.time())
    except Exception as e:
        print(f"DEBUG FOLLOWUP CHECK ERROR: {str(e)}")
