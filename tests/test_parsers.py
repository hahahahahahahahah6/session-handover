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


def _write_jsonl(path, objs):
    import json
    with open(path, "w", encoding="utf-8") as fh:
        for o in objs:
            fh.write(json.dumps(o) + "\n")


def _claude_msg(sid, role, content, ts="2026-10-01T00:00:00Z"):
    return {"type": "user" if role == "user" else "assistant",
            "sessionId": sid, "timestamp": ts, "cwd": "/x",
            "message": {"role": role, "content": content}}


def test_file_content_with_error_word_not_flagged():
    # Reading a source file that merely contains the word "error" must not
    # create an error entry.
    import tempfile
    objs = [
        _claude_msg("s1", "user", [{"type": "text", "text": "read the handler"}]),
        _claude_msg("s1", "assistant", [{"type": "tool_use", "id": "t1",
                                        "name": "Read",
                                        "input": {"file_path": "/x/h.py"}}]),
        _claude_msg("s1", "user", [{"type": "tool_result", "tool_use_id": "t1",
                                    "content": "def handle_error(e):\n"
                                               "    # error handling logic\n"
                                               "    log('an error occurred')\n"
                                               "    return 'failed to parse'"}]),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        _write_jsonl(fh.name, objs)
        path = fh.name
    try:
        s = parsers.parse_claude_code(path)
        assert s["errors"] == [], s["errors"]
    finally:
        os.remove(path)
    print("file-content error words not flagged ok")


def test_real_errors_still_flagged():
    import tempfile
    objs = [
        _claude_msg("s2", "user", [{"type": "text", "text": "run the tests"}]),
        _claude_msg("s2", "assistant", [{"type": "tool_use", "id": "t1",
                                        "name": "Bash",
                                        "input": {"command": "pytest"}}]),
        _claude_msg("s2", "user", [{"type": "tool_result", "tool_use_id": "t1",
                                    "content": "Traceback (most recent call "
                                               "last):\n  File \"t.py\"\n"
                                               "AssertionError: boom"}]),
        _claude_msg("s2", "user", [{"type": "tool_result", "tool_use_id": "t2",
                                    "content": "bash: frobnicate: command "
                                               "not found"}]),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        _write_jsonl(fh.name, objs)
        path = fh.name
    try:
        s = parsers.parse_claude_code(path)
        assert len(s["errors"]) == 2, s["errors"]
        assert any("Traceback" in e for e in s["errors"])
    finally:
        os.remove(path)
    print("real errors still flagged ok")


def test_goal_skips_tool_result_and_meta_first_messages():
    import tempfile
    objs = [
        _claude_msg("s3", "user", [{"type": "tool_result", "tool_use_id": "t0",
                                    "content": "stale output"}]),
        _claude_msg("s3", "user", [{"type": "text",
                                    "text": "<system-reminder>context low"
                                            "</system-reminder>"}]),
        _claude_msg("s3", "user", [{"type": "text",
                                    "text": "Migrate the DB to postgres"}]),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        _write_jsonl(fh.name, objs)
        path = fh.name
    try:
        s = parsers.parse_claude_code(path)
        assert s["goal"] == "Migrate the DB to postgres", repr(s["goal"])
    finally:
        os.remove(path)
    print("goal skips non-genuine first messages ok")


def test_discover_excludes_subagents_and_peek_matches():
    import tempfile
    d = tempfile.mkdtemp()
    main_objs = [_claude_msg("main1", "user", [{"type": "text",
                                                "text": "do the thing"}])]
    sub_objs = [{"type": "user", "sessionId": "sub1", "isSidechain": True,
                 "timestamp": "2026-10-01T00:00:00Z", "cwd": "/x",
                 "message": {"role": "user",
                             "content": [{"type": "text",
                                          "text": "child task"}]}}]
    _write_jsonl(os.path.join(d, "main.jsonl"), main_objs)
    _write_jsonl(os.path.join(d, "sub.jsonl"), sub_objs)
    old_c = os.environ.get("SESSION_HANDOVER_CLAUDE_DIR")
    old_x = os.environ.get("SESSION_HANDOVER_CODEX_DIR")
    os.environ["SESSION_HANDOVER_CLAUDE_DIR"] = d
    os.environ["SESSION_HANDOVER_CODEX_DIR"] = d + "-does-not-exist"
    try:
        full = parsers.discover()
        ids = [s["session_id"] for s in full]
        assert ids == ["main1"], ids
        fast = parsers.discover(fast=True)
        fids = [s["session_id"] for s in fast]
        assert fids == ["main1"], fids
        assert fast[0]["tool"] == "claude-code"
        assert fast[0]["messages"] == 1
        assert fast[0]["started"].startswith("2026-10-01")
    finally:
        if old_c is None:
            os.environ.pop("SESSION_HANDOVER_CLAUDE_DIR", None)
        else:
            os.environ["SESSION_HANDOVER_CLAUDE_DIR"] = old_c
        if old_x is None:
            os.environ.pop("SESSION_HANDOVER_CODEX_DIR", None)
        else:
            os.environ["SESSION_HANDOVER_CODEX_DIR"] = old_x
    print("discover excludes subagents / peek ok")


if __name__ == "__main__":
    test_parse_claude_code_fixture()
    test_parse_codex_fixture()
    test_malformed_lines_do_not_crash()
    test_missing_file_does_not_crash()
    test_find_session_prefix()
    test_file_content_with_error_word_not_flagged()
    test_real_errors_still_flagged()
    test_goal_skips_tool_result_and_meta_first_messages()
    test_discover_excludes_subagents_and_peek_matches()
    print("ALL PARSER TESTS PASSED")
