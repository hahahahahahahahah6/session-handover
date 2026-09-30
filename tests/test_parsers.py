"""Smoke tests for the transcript parsers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from session_handover import parsers  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def test_parse_claude_code_fixture():
    s = parsers.parse_claude_code(os.path.join(FIX, "claude_session.jsonl"))
    assert s["tool"] == "claude-code"
    assert s["session_id"] == "claude-abc123"
    assert s["goal"].startswith("Add a /health endpoint")
    assert s["messages"] == 8
    assert s["counts"]["edits"] == 1
    assert s["counts"]["writes"] == 1
    assert s["counts"]["commands"] == 1
    assert s["counts"]["reads"] == 1
    assert "/home/test/demo/server.py" in s["files"]
    assert "/home/test/demo/test_health.py" in s["files"]
    assert any("flask" in e.lower() for e in s["errors"])
    assert len(s["actions"]) >= 3
    print("claude parse ok")


def test_parse_codex_fixture():
    s = parsers.parse_codex(os.path.join(FIX, "codex_session.jsonl"))
    assert s["tool"] == "codex"
    assert s["session_id"] == "codex-xyz789"
    assert s["goal"].startswith("Refactor the database helper")
    assert s["counts"]["edits"] == 1       # apply_patch
    assert s["counts"]["commands"] == 1    # shell
    assert any("Connection refused" in e for e in s["errors"])
    print("codex parse ok")


def test_malformed_lines_do_not_crash():
    bad = os.path.join(FIX, "malformed.jsonl")
    with open(bad, "w") as fh:
        fh.write('{"type": "user", "message": {"role": "user", "content": "hi"}}\n')
        fh.write("this is not json\n")
        fh.write('{"type": "assistant", "message": "not-a-dict"}\n')
        fh.write('{"broken": \n')
        fh.write('{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}}\n')
    s = parsers.parse_claude_code(bad)
    assert s["messages"] == 2
    assert s["counts"]["commands"] == 1  # tool_use without input still counts
    os.remove(bad)
    s2 = parsers.parse_codex(bad)
    assert s2["messages"] == 0  # codex parser ignores claude-shaped lines
    print("malformed ok")


def test_missing_file_does_not_crash():
    s = parsers.parse_claude_code("/nonexistent/x.jsonl")
    assert s["messages"] == 0 and s["goal"] == ""
    s = parsers.parse_codex("/nonexistent/x.jsonl")
    assert s["messages"] == 0
    print("missing file ok")


def test_find_session_prefix():
    sessions = [
        {"session_id": "claude-abc123"},
        {"session_id": "codex-xyz789"},
    ]
    assert parsers.find_session(sessions, "claude-abc")["session_id"] == "claude-abc123"
    assert parsers.find_session(sessions, "nope") is None
    print("find_session ok")


if __name__ == "__main__":
    test_parse_claude_code_fixture()
    test_parse_codex_fixture()
    test_malformed_lines_do_not_crash()
    test_missing_file_does_not_crash()
    test_find_session_prefix()
    print("ALL PARSER TESTS PASSED")
