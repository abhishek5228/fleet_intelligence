"""Who is sending a message, and what they are allowed to do.

Meta signs the webhook, so the sender number is trustworthy; this module decides what that
number may do. Manager-only buttons (approve / reject / pick a vendor) must never work for
a driver or vendor who somehow obtains a button id.
"""
from dataclasses import dataclass
from typing import Optional

from fleet import state
from fleet.config import cfg
from fleet.directory import lookup_driver_phone
from fleet.phones import last10


@dataclass(frozen=True)
class Sender:
    raw: str      # as Meta sent it (digits, with country code) - used as the reply address
    ten: str      # last 10 digits - how numbers are compared everywhere in the app

    @classmethod
    def from_message(cls, msg: dict) -> "Sender":
        raw = str(msg.get("from", "") or "")
        return cls(raw=raw, ten=last10(raw))

    @property
    def known(self) -> bool:
        return bool(self.ten)

    @property
    def is_manager(self) -> bool:
        manager = last10(cfg.MANAGER_WHATSAPP_NUMBER)
        if not manager:
            # No manager configured: only the explicit dev escape hatch lets anyone act as one.
            return cfg.ALLOW_INSECURE_DEV and self.known
        return self.known and self.ten == manager

    @property
    def is_active_driver(self) -> bool:
        """A driver who has accepted (or been dispatched to) a repair."""
        return self.known and self.ten in state.ACTIVE_VEHICLE_BY_PHONE

    @property
    def is_registered_driver(self) -> bool:
        """A driver listed in DRIVER_WHATSAPP_MAPPING (may not have accepted a repair yet)."""
        return self.known and self.ten in cfg.DRIVER_PHONE_TO_VEHICLE

    @property
    def is_driver(self) -> bool:
        return self.is_active_driver or self.is_registered_driver

    def vehicle(self) -> Optional[str]:
        return state.ACTIVE_VEHICLE_BY_PHONE.get(self.ten) or cfg.DRIVER_PHONE_TO_VEHICLE.get(self.ten)

    def incident_id(self) -> Optional[str]:
        """The incident this sender is attached to, matching phone numbers by last 10 digits."""
        if not self.known:
            return None
        for phone, inc_id in state.INCIDENT_CONTEXTS.items():
            if last10(phone) == self.ten:
                return inc_id
        return None

    def owns_incident(self, incident_ctx: dict) -> bool:
        """True if this number is the registered driver of the incident's vehicle."""
        driver = lookup_driver_phone(incident_ctx.get("vehicle_id"))
        return bool(driver) and last10(driver) == self.ten
