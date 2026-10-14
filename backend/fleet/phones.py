"""Tiny phone-number helpers (WhatsApp sends digits only; configs often carry a '+')."""
from typing import Optional


def digits(phone: Optional[str]) -> str:
    """Keep only the digits of a phone number (spaces, dashes, brackets and '+' are dropped)."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def last10(phone: Optional[str]) -> str:
    """The last 10 digits — how this app compares numbers regardless of country-code format."""
    return digits(phone)[-10:]
