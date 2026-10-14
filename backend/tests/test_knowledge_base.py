"""Tests for the file-based knowledge base loader and the shared engine handle."""
import json

import pytest

from fleet import knowledge
from fleet.config import cfg


def _write(dirpath, name, payload):
    (dirpath / name).write_text(json.dumps(payload))


def _doc(i, **kw):
    return {"id": i, "manual": "M", "keywords": ["x"], "part_code": "P", "content": "C", **kw}


@pytest.fixture
def kb(tmp_path, monkeypatch):
    d = tmp_path / "knowledge_base"
    d.mkdir()
    monkeypatch.setattr(cfg, "KNOWLEDGE_BASE_DIR", str(d))
    return d


def test_loader_reads_list_and_single_object_files(kb):
    _write(kb, "doc1.json", [_doc("DOC-01")])
    _write(kb, "doc2.json", _doc("DOC-02"))
    assert {d["id"] for d in knowledge.load_knowledge_base()} == {"DOC-01", "DOC-02"}


def test_loader_skips_duplicate_ids(kb):
    _write(kb, "a.json", [_doc("DUP", manual="M1")])
    _write(kb, "b.json", [_doc("DUP", manual="M2")])
    docs = knowledge.load_knowledge_base()
    assert len(docs) == 1 and docs[0]["manual"] == "M1"


def test_loader_skips_malformed_entries_and_files(kb):
    _write(kb, "a.json", [_doc("OK"), {"id": "missing-fields"}, "a string", 42, None])
    (kb / "broken.json").write_text("{not json")
    (kb / "notes.txt").write_text("ignored")
    assert [d["id"] for d in knowledge.load_knowledge_base()] == ["OK"]


def test_loader_falls_back_to_seed_when_empty_or_missing(kb, monkeypatch):
    assert len(knowledge.load_knowledge_base()) >= 1
    monkeypatch.setattr(cfg, "KNOWLEDGE_BASE_DIR", str(kb / "does-not-exist"))
    assert len(knowledge.load_knowledge_base()) >= 1


def test_shipped_knowledge_base_is_valid(monkeypatch):
    """The real files under backend/knowledge_base must all load cleanly."""
    import os
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge_base")
    monkeypatch.setattr(cfg, "KNOWLEDGE_BASE_DIR", real)
    docs = knowledge.load_knowledge_base()
    assert docs and all({"id", "manual", "keywords", "part_code", "content"} <= set(d) for d in docs)
    assert len({d["id"] for d in docs}) == len(docs)


def test_engine_handle_can_be_injected_and_reset():
    sentinel = object()
    knowledge.set_engine(sentinel)
    assert knowledge.get_engine() is sentinel
    knowledge.set_engine(None)
