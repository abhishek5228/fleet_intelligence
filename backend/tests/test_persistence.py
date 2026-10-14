"""Tests for the SQLite-backed PersistentDict used for all webhook state."""
import threading

import pytest

from fleet.store import PersistentDict


@pytest.fixture
def pdict(tmp_path):
    d = PersistentDict("test_table", str(tmp_path / "test.db"))
    yield d
    d.close()


def test_set_and_get(pdict):
    pdict["key1"] = {"a": 1, "b": [1, 2, 3]}
    assert pdict["key1"] == {"a": 1, "b": [1, 2, 3]}


def test_missing_key_raises_and_get_defaults(pdict):
    with pytest.raises(KeyError):
        _ = pdict["nope"]
    assert pdict.get("nope", 7) == 7


def test_delete_len_iter_contains(pdict):
    pdict["a"], pdict["b"] = 1, 2
    assert len(pdict) == 2 and set(pdict) == {"a", "b"} and "a" in pdict
    del pdict["a"]
    assert "a" not in pdict and len(pdict) == 1
    with pytest.raises(KeyError):
        del pdict["a"]


def test_overwrite_keeps_one_row(pdict):
    pdict["k"] = 1
    pdict["k"] = 2
    assert pdict["k"] == 2 and len(pdict) == 1


def test_survives_across_instances(tmp_path):
    """The whole point of this class: a restart must still see previously written data."""
    path = str(tmp_path / "persist_test.db")
    d1 = PersistentDict("incidents", path)
    d1["INC-1"] = {"status": "PENDING"}
    d1.close()
    assert PersistentDict("incidents", path)["INC-1"] == {"status": "PENDING"}


def test_tables_are_independent(tmp_path):
    path = str(tmp_path / "t.db")
    a, b = PersistentDict("a", path), PersistentDict("b", path)
    a["k"] = 1
    assert "k" not in b


def test_nested_mutation_requires_explicit_reassign(pdict):
    """The ONE behavioural difference from a plain dict: .get() returns a COPY."""
    pdict["ctx"] = {"vendors": {"v1": {"price": None}}}
    ctx = pdict["ctx"]
    ctx["vendors"]["v1"]["price"] = 100
    assert pdict["ctx"]["vendors"]["v1"]["price"] is None
    pdict["ctx"] = ctx
    assert pdict["ctx"]["vendors"]["v1"]["price"] == 100


@pytest.mark.parametrize("bad", ["x; DROP TABLE y", "1abc", "a-b", "", "a b"])
def test_table_name_is_validated(tmp_path, bad):
    with pytest.raises(ValueError):
        PersistentDict(bad, str(tmp_path / "t.db"))


def test_creates_missing_parent_directory(tmp_path):
    d = PersistentDict("t", str(tmp_path / "nested" / "dir" / "x.db"))
    d["k"] = 1
    assert d["k"] == 1


def test_concurrent_writers_do_not_corrupt_or_crash(pdict):
    errors = []

    def work(n):
        try:
            for i in range(50):
                pdict[f"{n}-{i}"] = {"n": n, "i": i}
                _ = pdict.get(f"{n}-{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(pdict) == 8 * 50


def test_locked_read_modify_write_loses_no_updates():
    """The reason STATE_LOCK exists: concurrent read-modify-write must not drop increments."""
    from fleet import state

    @state.locked
    def bump():
        cur = state.APPROVAL_STATES.get("counter", 0)
        state.APPROVAL_STATES["counter"] = cur + 1

    threads = [threading.Thread(target=lambda: [bump() for _ in range(25)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state.APPROVAL_STATES["counter"] == 200


def test_locked_is_reentrant():
    from fleet import state

    @state.locked
    def inner():
        return 1

    @state.locked
    def outer():
        return inner() + 1
    assert outer() == 2
