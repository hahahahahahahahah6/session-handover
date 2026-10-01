"""Tests for the compaction diff audit (v0.2)."""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from session_handover import compact_audit  # noqa: E402
from session_handover import cli  # noqa: E402


def _write_jsonl(path, objs):
    with open(path, "w", encoding="utf-8") as fh:
        for o in objs:
            fh.write(json.dumps(o) + "\n")


def _claude_msg(sid, role, text, ts="2026-10-01T00:00:00Z"):
    return {"type": role, "sessionId": sid, "timestamp": ts, "cwd": "/x",
            "message": {"role": role,
                        "content": [{"type": "text", "text": text}]}, }


def _compact_fixture(path):
    """Claude transcript: 4 text turns, then a summary compaction boundary."""
    objs = [
        _claude_msg("c1", "user",
                    "Refactor the auth module. "
                    "Never push to main without asking me first. "
                    "Always keep the auth module under tests."),
        _claude_msg("c1", "assistant",
                    "I'll start by reading the auth module."),
        _claude_msg("c1", "user",
                    "Always run the full test suite before committing "
                    "anything."),
        _claude_msg("c1", "assistant",
                    "Got it. We'll use pytest for the tests."),
        {"type": "summary", "sessionId": "c1",
         "timestamp": "2026-10-01T01:00:00Z",
         "summary": "Refactored the auth module. Used pytest for tests."},
        _claude_msg("c1", "user", "Now add rate limiting."),
    ]
    _write_jsonl(path, objs)
    return path


def _env_point_at(tmp, claude_files, codex_files):
    cc = os.path.join(tmp, "cc")
    cx = os.path.join(tmp, "cx")
    os.makedirs(cc)
    os.makedirs(cx)
    for name, objs in claude_files:
        _write_jsonl(os.path.join(cc, name), objs)
    for name, objs in codex_files:
        _write_jsonl(os.path.join(cx, name), objs)
    old = (os.environ.get("SESSION_HANDOVER_CLAUDE_DIR"),
           os.environ.get("SESSION_HANDOVER_CODEX_DIR"))
    os.environ["SESSION_HANDOVER_CLAUDE_DIR"] = cc
    os.environ["SESSION_HANDOVER_CODEX_DIR"] = cx
    return old


def _restore_env(old):
    for key, val in zip(("SESSION_HANDOVER_CLAUDE_DIR",
                         "SESSION_HANDOVER_CODEX_DIR"), old):
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def test_boundary_detection():
    with tempfile.TemporaryDirectory() as tmp:
        path = _compact_fixture(os.path.join(tmp, "s.jsonl"))
        turns, boundaries = compact_audit.find_boundaries(path, "claude-code")
    assert len(boundaries) == 1, boundaries
    assert len(turns) == 5  # 4 pre-compact + 1 post-compact text turn
    assert "Refactored the auth module" in boundaries[0][1]
    print("boundary detection ok")


def test_continuation_preamble_is_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.jsonl")
        objs = [
            _claude_msg("c9", "user", "Never edit the vendor directory."),
            _claude_msg("c9", "user",
                        "This session is being continued from a previous "
                        "conversation that ran out of context. "
                        "Summary: refactored auth, kept vendor untouched."),
        ]
        _write_jsonl(path, objs)
        turns, boundaries = compact_audit.find_boundaries(path, "claude-code")
    assert len(boundaries) == 1
    assert len(turns) == 1
    print("continuation preamble ok")


def test_extraction_kinds_and_citations():
    with tempfile.TemporaryDirectory() as tmp:
        path = _compact_fixture(os.path.join(tmp, "s.jsonl"))
        turns, boundaries = compact_audit.find_boundaries(path, "claude-code")
        items = compact_audit.extract_items(turns[:boundaries[0][0]])
    kinds = {(i["kind"], i["turn"]) for i in items}
    assert ("rule", 1) in kinds
    assert ("rule", 3) in kinds
    assert ("decision", 4) in kinds
    texts = [i["text"] for i in items]
    assert any("Never push to main" in t for t in texts)
    assert any("We'll use pytest" in t for t in texts)
    print("extraction kinds/citations ok")


def test_dropped_reported_survived_not():
    with tempfile.TemporaryDirectory() as tmp:
        path = _compact_fixture(os.path.join(tmp, "s.jsonl"))
        result = compact_audit.audit(path, "claude-code")
    b = result["boundaries"][0]
    assert len(b["extracted"]) == 4, [i["text"] for i in b["extracted"]]
    dropped_texts = [i["text"] for i in b["dropped"]]
    survived_texts = [i["text"] for i in b["survived"]]
    # "Never push to main without asking me first." shares no tokens
    # with the summary -> dropped, with citation and restore snippet.
    assert any("Never push to main" in t for t in dropped_texts)
    # "Always run the full test suite..." -> "tests" vs "test" -> dropped.
    assert any("full test suite" in t for t in dropped_texts)
    # Token-close items survive and are not in the dropped list.
    assert any("keep the auth module under tests" in t
               for t in survived_texts)
    assert any("We'll use pytest" in t for t in survived_texts)
    assert not (set(dropped_texts) & set(survived_texts))
    drop = next(i for i in b["dropped"] if "Never push" in i["text"])
    assert drop["turn"] == 1
    assert drop["text"] in compact_audit.render_markdown(result, "c1")
    print("dropped/survived ok")


def test_no_boundary_fail_soft():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.jsonl")
        _write_jsonl(path, [_claude_msg("c2", "user", "Just do the thing.")])
        result = compact_audit.audit(path, "claude-code")
    assert result["boundaries"] == []
    assert "No compaction boundary found" in \
        compact_audit.render_terminal(result)
    assert "No compaction boundary found" in \
        compact_audit.render_markdown(result)
    print("no-boundary fail-soft ok")


def _run_cli(argv):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(argv)
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def test_cli_audit_compact_reports_drops():
    with tempfile.TemporaryDirectory() as tmp:
        old = _env_point_at(tmp, [], [])
        cc = os.environ["SESSION_HANDOVER_CLAUDE_DIR"]
        _compact_fixture(os.path.join(cc, "s.jsonl"))
        try:
            rc, out = _run_cli(["audit-compact", "--session", "c1"])
        finally:
            _restore_env(old)
    assert rc == 0, out
    assert "dropped=2" in out
    assert "DROPPED" in out
    assert "Never push to main" in out
    assert "restore:" in out
    print("cli audit-compact ok")


def test_cli_fail_on_drop_exit_codes():
    with tempfile.TemporaryDirectory() as tmp:
        old = _env_point_at(tmp, [], [])
        cc = os.environ["SESSION_HANDOVER_CLAUDE_DIR"]
        _compact_fixture(os.path.join(cc, "s.jsonl"))
        _write_jsonl(os.path.join(cc, "plain.jsonl"),
                     [_claude_msg("c2", "user", "Just do the thing.")])
        try:
            rc, _ = _run_cli(["audit-compact", "--session", "c1",
                              "--fail-on-drop"])
            assert rc == 1, "drops present -> exit 1"
            rc2, out2 = _run_cli(["audit-compact", "--session", "c2",
                                  "--fail-on-drop"])
            assert rc2 == 0, "no boundary -> still exit 0"
            assert "No compaction boundary found" in out2
            rc3, _ = _run_cli(["audit-compact", "--session", "c1"])
            assert rc3 == 0, "audit never blocks by default"
        finally:
            _restore_env(old)
    print("fail-on-drop exit codes ok")


def test_cli_out_writes_markdown():
    with tempfile.TemporaryDirectory() as tmp:
        old = _env_point_at(tmp, [], [])
        cc = os.environ["SESSION_HANDOVER_CLAUDE_DIR"]
        _compact_fixture(os.path.join(cc, "s.jsonl"))
        out = os.path.join(tmp, "AUDIT.md")
        try:
            rc, _ = _run_cli(["audit-compact", "--session", "c1",
                              "--out", out])
        finally:
            _restore_env(old)
        assert rc == 0
        body = open(out).read()
        assert "# Compaction Audit" in body
        assert "## Boundary 1" in body
        assert "### Dropped" in body
        assert "Suggested restore" in body
        assert "turn 1" in body
    print("cli --out ok")


def test_codex_boundary_detected_and_tolerated():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cx.jsonl")
        objs = [
            {"type": "session_meta", "timestamp": "2026-10-01T00:00:00Z",
             "payload": {"id": "cx9", "cwd": "/x"}},
            {"type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text",
                                      "text": "Never deploy on Fridays."}]}},
            {"type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text",
                                      "text": "Conversation compacted. "
                                              "Summary: refactored auth."}]}},
        ]
        _write_jsonl(path, objs)
        turns, boundaries = compact_audit.find_boundaries(path, "codex")
        assert len(boundaries) == 1, boundaries
        result = compact_audit.audit(path, "codex")
        assert len(result["boundaries"][0]["dropped"]) == 1
        assert "Never deploy" in \
            result["boundaries"][0]["dropped"][0]["text"]
        # Codex transcript with no marker -> fail-soft.
        path2 = os.path.join(tmp, "cx2.jsonl")
        objs2 = [o for o in objs if "compacted" not in json.dumps(o)]
        _write_jsonl(path2, objs2)
        result2 = compact_audit.audit(path2, "codex")
        assert result2["boundaries"] == []
    print("codex shape tolerance ok")


def test_paraphrase_close_match_survives():
    # High token overlap (documented threshold) -> survived: one word
    # dropped, rest intact.
    items = [{"kind": "rule",
              "text": "Always run the full test suite before committing "
                      "anything.",
              "turn": 1}]
    survived, dropped = compact_audit.check_survival(
        items, "Always run the full suite before committing.")
    assert len(survived) == 1 and not dropped
    print("paraphrase close match ok")


def test_reworded_rule_is_dropped():
    # Honest limitation: token-overlap is not semantic. A human would say
    # this rule survived, the audit says it dropped.
    items = [{"kind": "rule", "text": "Never push directly to the main branch",
              "turn": 1}]
    survived, dropped = compact_audit.check_survival(
        items, "All changes go through pull requests.")
    assert len(dropped) == 1 and not survived
    print("reworded rule dropped (documented limitation) ok")


def test_malformed_transcript_does_not_crash():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.jsonl")
        with open(path, "w") as fh:
            fh.write("not json at all\n")
            fh.write('{"type": "summary"}\n')  # no summary text
            fh.write('{"type": "user"}\n')
            fh.write('{"broken": \n')
        result = compact_audit.audit(path, "claude-code")
        assert result["boundaries"] == []
        compact_audit.render_terminal(result)
        compact_audit.render_markdown(result)
        result2 = compact_audit.audit("/nonexistent/x.jsonl", "claude-code")
        assert result2["boundaries"] == []
    print("malformed does not crash ok")


if __name__ == "__main__":
    test_boundary_detection()
    test_continuation_preamble_is_boundary()
    test_extraction_kinds_and_citations()
    test_dropped_reported_survived_not()
    test_no_boundary_fail_soft()
    test_cli_audit_compact_reports_drops()
    test_cli_fail_on_drop_exit_codes()
    test_cli_out_writes_markdown()
    test_codex_boundary_detected_and_tolerated()
    test_paraphrase_close_match_survives()
    test_reworded_rule_is_dropped()
    test_malformed_transcript_does_not_crash()
    print("ALL COMPACT-AUDIT TESTS PASSED")
