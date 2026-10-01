"""PreCompact hook: auto-write a handover BEFORE compaction runs.

When /compact fires, the session's durable state (rules, decisions, open
TODOs) needs a structured handover written *before* the summary replaces
context. Today that step is manual -- Silta's hand-written handoff+compaction
process, Recall's pre-compact hook, and the plan-mode Ask HN thread (people
manually splitting PLAN_*.md files) all prove the pain is real. v0.2 audits
what compaction dropped; this prevents the loss by writing the handover
automatically.

Protocol note: the PreCompact hook JSON shape (``session_id``,
``transcript_path`` on stdin) comes from community documentation of Claude
Code's hook protocol, not a stable API. Everything here is parsed
tolerantly -- unknown shapes degrade to a no-op, never a crash.

Two entry points:
  - ``hook_main()`` -- the actual hook body. Fail-open: any exception
    prints a stderr note and exits 0, never blocking compaction.
  - ``hook_install()`` / ``hook_uninstall()`` -- register or remove the
    PreCompact entry in ``~/.claude/settings.json`` (merge, idempotent).

Speed: the hook must not slow down compaction. Durable-item extraction is
capped to the most recent EXTRACTION_WINDOW text turns (documented below);
parsing is a single linear pass. No LLM, no network, stdlib only.
"""

import json
import os
import re
import sys

from . import parsers
from . import compact_audit
from . import handover

# How many recent text-bearing turns feed the durable-item checklist.
# The handover body itself is generated from the full transcript (linear
# pass); only the heuristic extraction is windowed, because regex
# extraction is the expensive part and rules stated 10k turns ago are
# rarely the ones compaction drops unnoticed. Documented here and in the
# README; tune via SESSION_HANDOVER_WINDOW if needed.
EXTRACTION_WINDOW = int(os.environ.get("SESSION_HANDOVER_WINDOW", "300"))

OUT_DIR_ENV = "SESSION_HANDOVER_DIR"


def output_dir():
    """Where HANDOVER.<session>.md files go."""
    custom = os.environ.get(OUT_DIR_ENV)
    if custom:
        return custom
    return os.path.join(os.path.expanduser("~"), ".cache",
                        "session-handover", "precompact")


def safe_session_id(session_id):
    """Sanitize a session id for use in a filename (no path traversal)."""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "")).strip("._")[:64]
    return s or "unknown"


def parse_hook_input(text):
    """Tolerantly parse PreCompact hook stdin.

    Returns a dict with ``session_id`` and ``transcript_path`` (either may
    be missing). Non-JSON or non-object input yields {} -- the caller
    treats that as a no-op, never an error.
    """
    try:
        obj = json.loads(text or "")
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    session_id = obj.get("session_id") or obj.get("sessionId") or ""
    transcript = (obj.get("transcript_path") or obj.get("transcriptPath")
                  or "")
    return {"session_id": str(session_id), "transcript_path": str(transcript)}


def _recent_turns(path, tool):
    """Text turns feeding the checklist, capped to EXTRACTION_WINDOW.

    Uses compact_audit.find_boundaries (never raises) and ignores any
    boundaries: at PreCompact time nothing has been compacted *yet* -- the
    whole transcript is the pre-compact context.
    """
    turns, _boundaries = compact_audit.find_boundaries(path, tool)
    return turns[-EXTRACTION_WINDOW:]


def durable_checklist(path, tool):
    """Extract durable items from the transcript's recent turns."""
    try:
        turns = _recent_turns(path, tool)
    except Exception:
        return []
    try:
        return compact_audit.extract_items(turns)
    except Exception:
        return []


def render_checklist(items):
    """Render the 'must preserve' checklist appended to the handover."""
    lines = []
    lines.append("## Durable items the next session must preserve")
    lines.append("")
    lines.append("_Written before compaction ran. The auto-summary may drop "
                 "these -- paste back anything the new session needs._")
    lines.append("")
    if not items:
        lines.append("_No explicitly-stated rules, TODOs, decisions, or "
                     "preferences were detected in the recent turns._")
        lines.append("")
    else:
        for item in items:
            lines.append("- [ ] **[%s]** (turn %d) %s" %
                         (item["kind"], item["turn"], item["text"]))
        lines.append("")
    lines.append("_Heuristic extraction (recall floor, token patterns, not "
                 "semantic). Subtly-phrased constraints may be missing -- "
                 "skim the timeline above._")
    lines.append("")
    return "\n".join(lines)


def run_hook(session_id, transcript_path, out_dir=None):
    """Generate the handover + checklist and write it to disk.

    Returns the path written. Never raises: on parse failure a degraded
    handover (checklist only, with a note) is still written when possible.
    """
    sid = safe_session_id(session_id)
    out_dir = out_dir or output_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "HANDOVER.%s.md" % sid)

    tool = "claude-code"
    try:
        tool = parsers.peek(transcript_path).get("tool") or "claude-code"
        if tool == "codex":
            session = parsers.parse_codex(transcript_path)
        else:
            tool = "claude-code"
            session = parsers.parse_claude_code(transcript_path)
        session["session_id"] = sid
        text = handover.generate(session)
        if not session.get("messages"):
            text += ("\n_No messages could be parsed from this transcript -- "
                     "the format may have drifted._\n")
    except Exception as e:
        text = ("# Session Handover\n\n_Transcript parse failed at "
                "PreCompact time (%s); the checklist below is extracted "
                "from raw text turns._\n\n" % _short_err(e))
    text += render_checklist(durable_checklist(transcript_path, tool))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _short_err(e):
    return " ".join(str(e).split())[:200]


def hook_main(stdin_text=None):
    """Hook body. Always exits 0 -- fail-open, never blocks compaction."""
    try:
        if stdin_text is None:
            stdin_text = sys.stdin.read()
        info = parse_hook_input(stdin_text)
        if not info.get("session_id"):
            sys.stderr.write("session-handover precompact: no session_id "
                             "in hook input; skipping.\n")
            return 0
        if not info.get("transcript_path") or not os.path.isfile(
                info["transcript_path"]):
            sys.stderr.write("session-handover precompact: no usable "
                             "transcript_path; skipping.\n")
            return 0
        path = run_hook(info["session_id"], info["transcript_path"])
        sys.stderr.write("session-handover precompact: wrote %s\n" % path)
        return 0
    except Exception as e:  # fail-open: hook must never block /compact
        sys.stderr.write("session-handover precompact hook skipped: %s\n"
                         % _short_err(e))
        return 0


# ---------------------------------------------------------------------------
# Hook installation into ~/.claude/settings.json
# ---------------------------------------------------------------------------

# Command registered in settings.json. Kept as a plain "python3 <shim>"
# string so idempotency checks can compare it exactly.
def _hook_command():
    return "python3 %s" % _shim_path()


def _package_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _shim_path():
    """Absolute path of hooks/precompact.py to register.

    1. If the package was installed from a repo checkout (editable install
       or running from source), hooks/precompact.py sits next to src/ --
       register that file directly.
    2. Otherwise materialize a generated shim into
       ~/.cache/session-handover/hooks/precompact.py and register that.
    Deterministic across runs in the same environment, so install/uninstall
    idempotency checks can match on the command string.
    """
    repo_shim = os.path.abspath(os.path.join(_package_dir(), "..", "..",
                                             "hooks", "precompact.py"))
    if os.path.isfile(repo_shim):
        return repo_shim
    dest_dir = os.path.join(os.path.expanduser("~"), ".cache",
                            "session-handover", "hooks")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "precompact.py")
    if not os.path.isfile(dest):
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(GENERATED_SHIM)
    return dest


# Self-contained fallback shim, used when the repo checkout's
# hooks/precompact.py is not on disk (e.g. plain pip install). Imports the
# installed package; fail-open if the package is ever uninstalled.
GENERATED_SHIM = '''#!/usr/bin/env python3
"""session-handover PreCompact hook shim (generated by `hook-install`).

Fail-open: never blocks compaction. Delegates to the installed
session_handover package; if that import fails, exits 0 with a note.
"""

import sys


def main():
    try:
        from session_handover.precompact import hook_main
        hook_main()
    except Exception as e:
        sys.stderr.write("session-handover precompact hook skipped: %s\\n" % e)
    sys.exit(0)


if __name__ == "__main__":
    main()
'''


def settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _load_settings(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data


def _save_settings(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _precompact_entries(data):
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return None
    entries = hooks.get("PreCompact")
    if not isinstance(entries, list):
        return None
    return entries


def hook_install():
    """Register the PreCompact hook in ~/.claude/settings.json.

    Merges with existing settings; never clobbers other hooks or events.
    Idempotent: re-running does not duplicate the entry. Returns the
    command string registered.
    """
    command = _hook_command()
    path = settings_path()
    data = _load_settings(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    entries = hooks.get("PreCompact")
    if not isinstance(entries, list):
        entries = []
        hooks["PreCompact"] = entries

    for entry in entries:
        nested = entry.get("hooks") if isinstance(entry, dict) else None
        if isinstance(nested, list):
            for h in nested:
                if isinstance(h, dict) and h.get("command") == command:
                    return command  # already installed

    entries.append({"hooks": [{"type": "command", "command": command}]})
    _save_settings(path, data)
    return command


def hook_uninstall():
    """Remove our PreCompact entry. Idempotent. Returns True if removed."""
    command = _hook_command()
    path = settings_path()
    data = _load_settings(path)
    entries = _precompact_entries(data)
    if entries is None:
        return False
    removed = False
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        nested = entry.get("hooks")
        if not isinstance(nested, list):
            kept.append(entry)
            continue
        remaining = [h for h in nested
                     if not (isinstance(h, dict)
                             and h.get("command") == command)]
        if len(remaining) != len(nested):
            removed = True
        if remaining:
            entry["hooks"] = remaining
            kept.append(entry)
        # else: drop the now-empty matcher entry
    hooks = data["hooks"]
    if kept:
        hooks["PreCompact"] = kept
    else:
        del hooks["PreCompact"]
    if removed:
        _save_settings(path, data)
    return removed
