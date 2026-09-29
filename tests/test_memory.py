"""Tests for markdown-file memory."""
from datetime import date

import pytest

from agentkai.memory import Memory


@pytest.fixture()
def mem(tmp_path):
    return Memory(root=tmp_path / "memory")


def test_write_section_aliases(mem):
    mem.write("soul", "You are Kai.")
    mem.write("user", "The user likes tea.")
    mem.write("memory", "Fact one.")
    assert mem.read("SOUL.md") == "You are Kai."
    assert mem.read("USER.md") == "The user likes tea."
    assert mem.read("MEMORY.md") == "Fact one."
    # literal filenames still work (backwards compatible)
    mem.write("SOUL.md", "v2")
    assert mem.read("SOUL.md") == "v2"


def test_append_daily(mem):
    mem.append_daily("learned something")
    text = mem.read(f"{date.today().isoformat()}.md")
    assert "learned something" in text
    assert text.startswith("- [")


def test_log_day_alias(mem):
    mem.log_day("alias works")
    assert "alias works" in mem.read_daily()


def test_context_block(mem):
    mem.write("soul", "persona")
    mem.write("user", "prefs")
    block = mem.context_block()
    assert "[SOUL.md]\npersona" in block
    assert "[USER.md]\nprefs" in block
    assert "MEMORY.md" not in block  # empty files are skipped


# ---- people / groups ----------------------------------------------------------


def test_add_person_idempotent(mem):
    p1 = mem.add_person("Muhammad Araf", "co-built okfsmith")
    p2 = mem.add_person("Muhammad Araf")
    assert p1 == p2
    assert "co-built okfsmith" in p1.read_text()
    assert mem.people() == {"Muhammad Araf": "people/muhammad-araf.md"}


def test_person_lookup_creates_stub(mem):
    content = mem.person("Someone New")
    assert "# Someone New" in content
    # second lookup finds it (case-insensitive) without duplicating
    assert mem.person("someone new") == content
    assert len(mem.people()) == 1


def test_add_group(mem):
    mem.add_group("OKF team", "open-source build team")
    assert mem.groups() == {"OKF team": "groups/okf-team.md"}
    assert "open-source build team" in mem.group("okf team")


def test_index_file_is_human_readable(mem):
    mem.add_person("Ada Lovelace", "first programmer")
    index = (mem.root / "people" / "INDEX.md").read_text()
    assert "Ada Lovelace" in index
    assert "people/ada-lovelace.md" in index


# ---- search -------------------------------------------------------------------


def test_search_ranking(mem):
    mem.write("memory", "blorpt appears here\nblorpt appears twice blorpt\n")
    mem.write("soul", "nothing relevant\n")
    (mem.root / "2026-01-01.md").write_text("a single blorpt\n")
    hits = mem.search("blorpt")
    assert hits, "expected hits"
    # MEMORY.md line with 2 occurrences outranks the daily log's single one
    assert hits[0]["file"] == "MEMORY.md"
    assert hits[0]["lineno"] == 2
    # refs are file:line
    ref = f"{hits[0]['file']}:{hits[0]['lineno']}"
    assert ref == "MEMORY.md:2"
    # scores descend
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_search_ignores_short_terms(mem):
    mem.write("memory", "some content here")
    assert mem.search("a") == []
    assert mem.search("") == []


def test_search_multi_term(mem):
    mem.write("memory", "the quick brown fox\nlazy dog sleeps\n")
    hits = mem.search("quick fox")
    assert hits and hits[0]["lineno"] == 1


def test_search_missing_root_is_empty(tmp_path):
    m = Memory(root=tmp_path / "fresh")
    assert m.search("anything") == []
