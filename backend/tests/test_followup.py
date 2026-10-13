"""Proactive follow-up timers."""
import time

from conftest import DRIVER, MANAGER, VEHICLE
from fleet import followup, state


def test_schedule_arms_one_daemon_timer(wa, incident, timers):
    followup.schedule_followup_check(incident)
    t = timers.instances[0]
    assert (t.interval, t.daemon, t.started) == (7200, True, True)
    assert state.DRIVER_LAST_UPDATE_TS[incident] > 0


def test_silent_driver_gets_a_check_in_and_the_chain_continues(wa, incident, timers):
    followup.schedule_followup_check(incident)
    timers.instances[0].fire()
    assert any("kaisi chal rahi hai" in t for t in wa.texts(DRIVER))
    assert any("Follow-up" in t for t in wa.texts(MANAGER))
    assert len(timers.instances) == 2


def test_driver_who_updated_meanwhile_is_not_pestered(wa, incident, timers):
    followup.schedule_followup_check(incident)
    state.DRIVER_LAST_UPDATE_TS[incident] = time.time() + 10
    timers.instances[0].fire()
    assert wa.calls == [] and len(timers.instances) == 2        # quiet, but keeps watching


def test_rejected_or_unknown_incidents_stop_the_chain(wa, incident, timers):
    state.APPROVAL_STATES[incident] = "REJECTED_REROUTING"
    followup.run_followup_check(incident, 0)
    followup.run_followup_check("INC-GONE", 0)
    assert wa.calls == [] and timers.instances == []


def test_failure_inside_the_timer_is_contained(wa, incident, timers, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("send failed")
    monkeypatch.setattr(followup, "send_whatsapp_text_reply", boom)
    followup.run_followup_check(incident, 0)       # must not raise on the timer thread
    assert VEHICLE
