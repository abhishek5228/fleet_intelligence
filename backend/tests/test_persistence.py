"""Tests for the SQLite-backed PersistentDict — the drop-in dict replacement
used for APPROVAL_STATES / INCIDENT_DETAILS / PENDING_QUOTES."""
import os
import pytest


@pytest.fixture
def persistent_dict(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.db"))
    return main.PersistentDict("test_table")


def test_set_and_get(persistent_dict):
    persistent_dict["key1"] = {"a": 1, "b": [1, 2, 3]}
    assert persistent_dict["key1"] == {"a": 1, "b": [1, 2, 3]}


def test_missing_key_raises(persistent_dict):
    with pytest.raises(KeyError):
        _ = persistent_dict["nope"]


def test_delete(persistent_dict):
    persistent_dict["key1"] = "value"
    del persistent_dict["key1"]
    assert "key1" not in persistent_dict


def test_survives_across_instances(tmp_path, monkeypatch):
    """The whole point of this class: a second 'connection' (simulating a
    restart) must still see previously written data."""
    import main
    db_path = str(tmp_path / "persist_test.db")
    monkeypatch.setattr(main, "DB_PATH", db_path)

    d1 = main.PersistentDict("incidents")
    d1["INC-1"] = {"status": "PENDING"}
    del d1

    d2 = main.PersistentDict("incidents")  # fresh "connection"
    assert d2["INC-1"] == {"status": "PENDING"}


def test_nested_mutation_requires_explicit_reassign(persistent_dict):
    """Documents the ONE real behavioral difference from a plain dict: nested
    mutation is NOT automatically persisted — callers must reassign the
    top-level key. (This is the exact pattern _handle_vendor_quote_reply
    follows.)"""
    persistent_dict["ctx"] = {"vendors": {"v1": {"price": None}}}
    ctx = persistent_dict["ctx"]
    ctx["vendors"]["v1"]["price"] = 100  # mutate the fetched COPY
    # Without reassignment, the store is untouched:
    assert persistent_dict["ctx"]["vendors"]["v1"]["price"] is None
    # With explicit reassignment (the pattern used throughout main.py), it sticks:
    persistent_dict["ctx"] = ctx
    assert persistent_dict["ctx"]["vendors"]["v1"]["price"] == 100
