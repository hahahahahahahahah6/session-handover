"""Tests for the cross-harness compaction matrix (v0.5) and the Codex
checkpoint-marker improvements to the compaction audit."""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from session_handover import compact_audit  # noqa: E402
from session_handover import compaction_matrix  # noqa: E402
from session_handover import cli  # noqa: E402


def _write_jsonl(path, objs):
    with open(path, "w", encoding="utf-8") as fh:
        for o in objs:
            fh.write(json.dumps(o) + "\n")


def _codex_user(text):
    return {"type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text",
                                     "text": text}]}}


def _codex_assistant(text):
    return {"type": "response_item",
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text",
                                     "text": text}]}}


# --- registry ------------------------------------------------------------

def test_matrix_has_expected_harnesses():
    ids = compaction_matrix.harnesses()
    for expected in ("claude-code", "codex-cli", "gemini-cli", "aider",
                     "opencode", "copilot-cli", "cursor", "cline"):
        assert expected in ids, "missing harness %r" % expected
    print("harness list ok (%d)" % len(ids))


def test_every_entry_has_required_fields():
    required = ("id", "name", "trigger", "mechanism", "survives", "drops",
                "anchor", "markers", "audit", "confidence", "evidence")
    for hid in compaction_matrix.harnesses():
        h = compaction_matrix.get(hid)
        for field in required:
            assert field in h, "%s missing %r" % (hid, field)
        assert h["survives"], "%s: empty survives" % hid
        assert h["drops"], "%s: empty drops" % hid
        assert h["anchor"].strip(), "%s: empty anchor" % hid
        assert h["audit"] in compaction_matrix.AUDIT_LEVELS, hid
        assert h["confidence"] in compaction_matrix.CONFIDENCE_LEVELS, hid
    print("entry fields ok")


def test_claude_code_entry_grounds_rulestack_evidence():
    h = compaction_matrix.get("claude-code")
    joined = " ".join(h["survives"]).lower()
    assert "claude.md" in joined, "CLAUDE.md must be listed as surviving"
    drops = " ".join(h["drops"]).lower()
    assert "path-scoped" in drops or "paths" in drops
    assert "8" in h["evidence"], "rulestack's 8 measured compactions"
    assert "6/8" in h["evidence"], "skill body 6/8 survival"
    assert h["audit"] == "full"
    assert h["confidence"] == "measured"
    print("claude-code evidence ok")


def test_codex_entry_grounds_remote_v2_semantics():
    h = compaction_matrix.get("codex-cli")
    drops = " ".join(h["drops"]).lower()
    assert "assistant messages" in drops, \
        "local fallback drops assistant messages"
    survives = " ".join(h["survives"]).lower()
    assert "retained user messages" in survives
    assert h["audit"] == "partial"
    assert "compact_prompt" in h["anchor"], "v0.50 configurable prompt"
    print("codex-cli evidence ok")


def test_get_unknown_harness_raises_helpful():
    try:
        compaction_matrix.get("windsurf")
    except ValueError as e:
        assert "claude-code" in str(e), "must list known ids"
    else:
        raise AssertionError("expected ValueError")
    print("unknown harness error ok")


def test_compare_claude_vs_codex_shows_asymmetric_drops():
    c = compaction_matrix.compare("claude-code", "codex-cli")
    a_only = " ".join(c["drops_only_in_a"]).lower()
    b_only = " ".join(c["drops_only_in_b"]).lower()
    # Claude-specific: path-scoped rules / nested CLAUDE.md.
    assert "path-scoped" in a_only or "nested" in a_only
    # Codex-specific: assistant messages.
    assert "assistant" in b_only
    # Anchors travel with the comparison.
    assert c["anchor_a"] and c["anchor_b"]
    assert c["audit_a"] == "full" and c["audit_b"] == "partial"
    print("compare ok")


def test_compare_unknown_id_fails_loud():
    try:
        compaction_matrix.compare("claude-code", "nope")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    print("compare unknown ok")


# --- rendering -----------------------------------------------------------

def test_render_terminal_table():
    text = compaction_matrix.render_terminal()
    for hid in compaction_matrix.harnesses():
        assert hid in text, "terminal table missing %r" % hid
    assert "TRIGGER" in text
    print("terminal table ok")


def test_render_markdown_full_report():
    md = compaction_matrix.render_markdown()
    assert md.startswith("# Compaction Semantics Matrix")
    for hid in compaction_matrix.harnesses():
        h = compaction_matrix.get(hid)
        assert ("## %s" % h["name"]) in md
    assert "rulestack" in md.lower() or "8 measured compactions" in md
    assert "Transcript audit" in md
    # Pairwise section appends on request.
    md2 = compaction_matrix.render_markdown(
        compare_pair=("claude-code", "codex-cli"))
    assert "Pairwise:" in md2
    assert "Drops unique to" in md2
    print("markdown report ok")


# --- codex checkpoint fixtures --------------------------------------------

def test_codex_remote_v1_checkpoint_is_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cx.jsonl")
        _write_jsonl(path, [
            _codex_user("Never deploy on Fridays."),
            _codex_assistant("Got it, noted."),
            _codex_user("/compact"),
            _codex_assistant("**CONTEXT CHECKPOINT - 2026-10-01** "
                             "Refactored auth to use connection pooling."),
        ])
        turns, boundaries = compact_audit.find_boundaries(path, "codex")
        assert len(boundaries) == 1, boundaries
        assert "CONTEXT CHECKPOINT" in boundaries[0][1]
        result = compact_audit.audit(path, "codex")
        dropped = result["boundaries"][0]["dropped"]
        assert len(dropped) == 1, dropped
        assert "Never deploy" in dropped[0]["text"]
    print("codex remote v1 checkpoint ok")


def test_codex_local_fallback_prefix_is_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cx2.jsonl")
        _write_jsonl(path, [
            _codex_user("Always run the full test suite before committing."),
            _codex_user("/compact"),
            _codex_user("Another language model started to solve this "
                        "problem and produced a summary of its thinking "
                        "process. Summary: fixed the flaky test."),
        ])
        turns, boundaries = compact_audit.find_boundaries(path, "codex")
        assert len(boundaries) == 1, boundaries
        result = compact_audit.audit(path, "codex")
        dropped = result["boundaries"][0]["dropped"]
        assert len(dropped) == 1, dropped
        assert "test suite" in dropped[0]["text"]
    print("codex local fallback prefix ok")


def test_codex_compaction_item_type_is_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cx3.jsonl")
        _write_jsonl(path, [
            _codex_user("Never deploy on Fridays."),
            {"type": "response_item",
             "payload": {"type": "compaction",
                         "text": "Summary: refactored auth module."}},
        ])
        _, boundaries = compact_audit.find_boundaries(path, "codex")
        assert len(boundaries) == 1, boundaries
    print("codex compaction item ok")


def test_codex_bare_compact_command_is_not_a_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cx4.jsonl")
        _write_jsonl(path, [
            _codex_user("Never deploy on Fridays."),
            _codex_user("/compact"),
            # User cancelled / no checkpoint followed: plain new turn.
            _codex_user("Actually, keep going on the auth refactor."),
        ])
        _, boundaries = compact_audit.find_boundaries(path, "codex")
        assert boundaries == [], boundaries
    print("codex bare /compact ignored ok")


# --- CLI smoke ------------------------------------------------------------

def _run_cli(argv):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(argv)
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def test_cli_compact_matrix_runs():
    rc, out = _run_cli(["compact-matrix"])
    assert rc == 0, rc
    assert "claude-code" in out and "codex-cli" in out
    print("cli compact-matrix ok")


def test_cli_compact_matrix_compare():
    rc, out = _run_cli(["compact-matrix", "--compare",
                        "claude-code", "codex-cli"])
    assert rc == 0, rc
    assert "drops only in" in out
    print("cli compare ok")


def test_cli_compact_matrix_harness_detail():
    rc, out = _run_cli(["compact-matrix", "--harness", "codex-cli"])
    assert rc == 0, rc
    assert "CONTEXT CHECKPOINT" in out
    print("cli harness detail ok")


def test_cli_compact_matrix_unknown_harness():
    rc, out = _run_cli(["compact-matrix", "--harness", "nope"])
    assert rc == 2, rc
    print("cli unknown harness ok")


def test_cli_compact_matrix_out_writes_markdown():
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "MATRIX.md")
        rc, _ = _run_cli(["compact-matrix", "--out", out_path])
        assert rc == 0, rc
        with open(out_path, encoding="utf-8") as fh:
            content = fh.read()
        assert content.startswith("# Compaction Semantics Matrix")
        assert "| Harness | Trigger |" in content
    print("cli --out ok")


if __name__ == "__main__":
    test_matrix_has_expected_harnesses()
    test_every_entry_has_required_fields()
    test_claude_code_entry_grounds_rulestack_evidence()
    test_codex_entry_grounds_remote_v2_semantics()
    test_get_unknown_harness_raises_helpful()
    test_compare_claude_vs_codex_shows_asymmetric_drops()
    test_compare_unknown_id_fails_loud()
    test_render_terminal_table()
    test_render_markdown_full_report()
    test_codex_remote_v1_checkpoint_is_boundary()
    test_codex_local_fallback_prefix_is_boundary()
    test_codex_compaction_item_type_is_boundary()
    test_codex_bare_compact_command_is_not_a_boundary()
    test_cli_compact_matrix_runs()
    test_cli_compact_matrix_compare()
    test_cli_compact_matrix_harness_detail()
    test_cli_compact_matrix_unknown_harness()
    test_cli_compact_matrix_out_writes_markdown()
    print("ALL COMPACT-MATRIX TESTS PASSED")
