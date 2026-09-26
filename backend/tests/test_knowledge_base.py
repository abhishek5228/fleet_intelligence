"""Tests for the file-based knowledge base loader in main.py."""
import json
import os
import pytest


@pytest.fixture
def temp_kb_dir(tmp_path, monkeypatch):
    kb_dir = tmp_path / "knowledge_base"
    kb_dir.mkdir()
    (kb_dir / "doc1.json").write_text(json.dumps([
        {"id": "DOC-01", "manual": "Test Manual", "keywords": ["overheat"],
         "part_code": "Part-1", "content": "Test content"}
    ]))
    (kb_dir / "doc2.json").write_text(json.dumps(
        {"id": "DOC-02", "manual": "Test Manual 2", "keywords": ["brake"],
         "part_code": "Part-2", "content": "Test content 2"}
    ))
    return str(kb_dir)


def test_loader_reads_multiple_json_files(temp_kb_dir):
    import main
    monkey_dir = main.KNOWLEDGE_BASE_DIR
    main.KNOWLEDGE_BASE_DIR = temp_kb_dir
    try:
        docs = main._load_knowledge_base()
        ids = {d["id"] for d in docs}
        assert ids == {"DOC-01", "DOC-02"}
    finally:
        main.KNOWLEDGE_BASE_DIR = monkey_dir


def test_loader_skips_duplicate_ids(tmp_path):
    import main
    kb_dir = tmp_path / "kb2"
    kb_dir.mkdir()
    (kb_dir / "a.json").write_text(json.dumps([{"id": "DUP", "manual": "M1", "keywords": ["x"], "part_code": "P1", "content": "C1"}]))
    (kb_dir / "b.json").write_text(json.dumps([{"id": "DUP", "manual": "M2", "keywords": ["y"], "part_code": "P2", "content": "C2"}]))
    monkey_dir = main.KNOWLEDGE_BASE_DIR
    main.KNOWLEDGE_BASE_DIR = str(kb_dir)
    try:
        docs = main._load_knowledge_base()
        assert len(docs) == 1  # second DUP skipped
    finally:
        main.KNOWLEDGE_BASE_DIR = monkey_dir


def test_loader_falls_back_to_seed_when_empty(tmp_path):
    import main
    kb_dir = tmp_path / "empty_kb"
    kb_dir.mkdir()
    monkey_dir = main.KNOWLEDGE_BASE_DIR
    main.KNOWLEDGE_BASE_DIR = str(kb_dir)
    try:
        docs = main._load_knowledge_base()
        assert len(docs) >= 1  # built-in seed kicks in
    finally:
        main.KNOWLEDGE_BASE_DIR = monkey_dir
