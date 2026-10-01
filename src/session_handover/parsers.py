"""Defensive parsers for coding-agent session transcripts.

Transcript formats are UNDOCUMENTED and change over time. Every parser here
is written to degrade gracefully: malformed lines are skipped, unknown shapes
are ignored, and a parse never raises on real-world input. If a transcript
format drifts, you get less detail -- not a crash.
"""

import json
import os
import re


def _lines(path):
    """Yield parsed JSON objects from a .jsonl file, skipping bad lines."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _first_text(blocks):
    """Pull the first text-ish string out of a content block list."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") in ("text", "input_text", "output_text"):
            t = b.get("text")
            if t:
                return str(t)
    return ""


_ERROR_RE = re.compile(
    r"traceback"              # Python stack traces
    r"|exception"             # SomethingException / "exception raised"
    r"|error\s*:"             # "Error: ...", "ValueError: ...", "AssertionError: ..."
    r"|failed\s+(with|exit)"  # "failed with exit code N", "command failed"
    r"|exit\s*code\s*[1-9]"   # non-zero exit codes
    r"|exited\s+with"         # "exited with status 1"
    r"|command not found"
    r"|permission denied"
    r"|\benoent\b"
    r"|\bpanic\b",            # Go/Rust panics
    re.IGNORECASE,
)


# Injected meta wrappers Claude Code adds around tool output / reminders --
# never a real user goal.
_META_PREFIXES = ("<system-reminder", "<local-command-caveat", "<command-name")


def _is_genuine_user_text(text):
    """True for substantive user-authored text (not tool_result-only, empty,
    or injected meta wrappers)."""
    t = (text or "").strip()
    if not t:
        return False
    lowered = t.lower()
    return not lowered.startswith(_META_PREFIXES)


# Tool-name hints identifying shell/command runners. The exception-keyword
# heuristic below is ONLY applied to results from these tools: running it
# over Read/Edit/Write output turns ordinary source text (e.g. a .py file
# containing "except Exception") into phantom errors.
_SHELL_HINTS = ("shell", "exec", "bash", "command", "run")


def _is_shell_tool(tool_name):
    """True when a tool result came from a shell/command runner.

    Unknown/empty tool names (truncated transcript, malformed tool_use)
    return True: for a result we cannot attribute, keep the legacy keyword
    heuristic rather than silently dropping a real error.
    """
    if not tool_name:
        return True
    return any(k in str(tool_name).lower() for k in _SHELL_HINTS)


def _looks_like_error(text):
    """True only for text that actually indicates a failure.

    Deliberately strict: a bare word like "error" shows up in ordinary file
    content and prose ("error handling"), which used to turn every Read of
    such a file into a false error entry.
    """
    if not text:
        return False
    return bool(_ERROR_RE.search(text))


# ---------------------------------------------------------------------------
# Claude Code: ~/.claude/projects/<slug>/<session-id>.jsonl
# Lines: {"type": "user"|"assistant"|"summary", "message": {...},
#         "timestamp": "...", "sessionId": "...", "cwd": "..."}
# assistant message content blocks: {"type": "text"} or
#   {"type": "tool_use", "name": "Edit"|"Write"|"Bash"|..., "input": {...}}
# ---------------------------------------------------------------------------

_CLAUDE_FILE_ACTIONS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_CLAUDE_READ_ACTIONS = {"Read", "Glob", "Grep"}


def parse_claude_code(path):
    actions = []       # chronological notable actions
    errors = []
    first_user = ""
    user_count = 0
    assistant_count = 0
    started = ""
    cwd = ""
    session_id = os.path.splitext(os.path.basename(path))[0]
    counts = {"edits": 0, "writes": 0, "commands": 0, "reads": 0}
    files = []
    tool_names = {}  # tool_use id -> tool name, to attribute tool_results

    for obj in _lines(path):
        started = started or obj.get("timestamp", "")
        cwd = cwd or obj.get("cwd", "")
        session_id = obj.get("sessionId") or session_id
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", obj.get("type", ""))
        content = msg.get("content", [])

        if role == "user":
            user_count += 1
            if not first_user:
                # The first user message may be a tool_result-only or
                # injected meta message; the goal is the first substantive
                # user-authored text.
                candidate = _first_text(content)
                if _is_genuine_user_text(candidate):
                    first_user = candidate.strip()[:2000]
            # tool results may carry errors. The exception-keyword heuristic
            # is only meaningful for shell output: for Read/Edit/Write and
            # friends the error status comes solely from is_error, so that
            # ordinary source text (e.g. "except Exception" in a .py file)
            # is never logged as an error.
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        txt = c if isinstance(c, str) else _first_text(c)
                        from_shell = _is_shell_tool(
                            tool_names.get(b.get("tool_use_id"), ""))
                        if b.get("is_error") or (from_shell
                                                 and _looks_like_error(txt)):
                            errors.append(txt[:500])
        elif role == "assistant":
            assistant_count += 1
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        name = b.get("name", "tool")
                        if b.get("id"):
                            tool_names[b["id"]] = name
                        inp = b.get("input") or {}
                        if not isinstance(inp, dict):
                            inp = {}
                        if name in _CLAUDE_FILE_ACTIONS:
                            counts["edits" if name in ("Edit", "MultiEdit") else "writes"] += 1
                            p = inp.get("file_path") or inp.get("path") or ""
                            if p and p not in files:
                                files.append(p)
                            actions.append({"kind": "file", "detail": "%s %s" % (name, p or "(unknown path)")})
                        elif name == "Bash":
                            counts["commands"] += 1
                            cmd = str(inp.get("command", ""))[:160]
                            actions.append({"kind": "command", "detail": cmd or "(empty command)"})
                        elif name in _CLAUDE_READ_ACTIONS:
                            counts["reads"] += 1
                        else:
                            actions.append({"kind": "tool", "detail": name})

    return {
        "tool": "claude-code",
        "path": path,
        "session_id": session_id,
        "started": started,
        "cwd": cwd,
        "messages": user_count + assistant_count,
        "goal": first_user,
        "actions": actions,
        "errors": errors[:10],
        "files": files,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Codex CLI: ~/.codex/sessions/YYYY/MM/DD/<id>.jsonl
# Lines: {"type": "session_meta", ...},
#        {"type": "response_item", "payload": {"type": "message"|"function_call"|
#          "function_call_output"|"reasoning", ...}}
# Best-effort: the Codex CLI format has changed across versions.
# ---------------------------------------------------------------------------

def _codex_args_summary(name, arguments):
    """Summarize a function_call's arguments into a short human string."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            return arguments[:160]
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "cmd", "path", "file", "filename", "workdir"):
        if arguments.get(key):
            return "%s %s" % (name, str(arguments[key])[:140])
    # fallback: first scalar value
    for v in arguments.values():
        if isinstance(v, (str, int, float)):
            return "%s %s" % (name, str(v)[:140])
    return name


def parse_codex(path):
    actions = []
    errors = []
    first_user = ""
    user_count = 0
    assistant_count = 0
    started = ""
    cwd = ""
    session_id = os.path.splitext(os.path.basename(path))[0]
    counts = {"edits": 0, "writes": 0, "commands": 0, "reads": 0}
    files = []
    call_names = {}  # function_call call_id -> function name

    for obj in _lines(path):
        otype = obj.get("type", "")
        if otype == "session_meta":
            meta = obj.get("payload") or {}
            started = started or obj.get("timestamp", "") or meta.get("timestamp", "")
            cwd = cwd or meta.get("cwd", "") or obj.get("cwd", "")
            session_id = meta.get("id") or session_id
            continue
        if otype != "response_item":
            continue
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        ptype = p.get("type", "")
        started = started or obj.get("timestamp", "")

        if ptype == "message":
            role = p.get("role", "")
            text = _first_text(p.get("content", []))
            if role == "user":
                user_count += 1
                if not first_user and _is_genuine_user_text(text):
                    first_user = text.strip()[:2000]
                # Note: no keyword-based error detection on user messages --
                # the heuristic is reserved for shell tool output only, so a
                # user pasting a traceback to ask about it is not logged as
                # a session error.
            elif role == "assistant":
                assistant_count += 1
        elif ptype == "function_call":
            name = p.get("name", "tool")
            if p.get("call_id"):
                call_names[p["call_id"]] = name
            summary = _codex_args_summary(name, p.get("arguments", ""))
            nl = name.lower()
            if any(k in nl for k in ("patch", "edit", "write", "apply")):
                counts["edits"] += 1
                m = re.search(r"[\w\-./]+\.\w+", summary)
                if m and m.group(0) not in files:
                    files.append(m.group(0))
                actions.append({"kind": "file", "detail": summary or name})
            elif any(k in nl for k in ("shell", "exec", "bash", "command", "run")):
                counts["commands"] += 1
                actions.append({"kind": "command", "detail": summary or name})
            elif any(k in nl for k in ("read", "view", "cat", "glob", "grep")):
                counts["reads"] += 1
            else:
                actions.append({"kind": "tool", "detail": summary or name})
        elif ptype == "function_call_output":
            out = p.get("output")
            txt = out if isinstance(out, str) else str(out or "")[:500]
            status = str(p.get("status", "")).lower()
            bad_status = status not in ("", "success", "completed", "ok")
            # Keyword heuristic only for shell output; other tools are
            # judged by status alone so their output text (e.g. source
            # code mentioning "exception") is never a phantom error.
            from_shell = _is_shell_tool(call_names.get(p.get("call_id"), ""))
            if bad_status or (from_shell and _looks_like_error(txt)):
                errors.append(txt[:500])
        # "reasoning" and anything else: intentionally ignored

    return {
        "tool": "codex",
        "path": path,
        "session_id": session_id,
        "started": started,
        "cwd": cwd,
        "messages": user_count + assistant_count,
        "goal": first_user,
        "actions": actions,
        "errors": errors[:10],
        "files": files,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _is_subagent_transcript(path):
    """Best-effort detection of subagent (Task sidechain) transcripts.

    Child agent runs are not sessions a user would hand over; listing them
    as independent sessions is noise. Markers are checked defensively on raw
    lines (no JSON parse needed) -- unknown markers simply never match.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 200:
                    break
                if '"isSidechain": true' in line or '"isSidechain":true' in line:
                    return True
                if '"parentSessionId"' in line:
                    return True
    except OSError:
        pass
    return False


def peek(path):
    """Cheap metadata scan for `list`: session_id, tool, started, cwd and an
    approximate message count. Reads the file once but does no block-level
    parsing, no error regex scans, and builds no action lists. Never raises.
    """
    info = {
        "tool": "?",
        "path": path,
        "session_id": os.path.splitext(os.path.basename(path))[0],
        "started": "",
        "cwd": "",
        "messages": 0,
    }
    try:
        for obj in _lines(path):
            otype = obj.get("type", "")
            if otype in ("user", "assistant"):
                info["tool"] = "claude-code"
                info["messages"] += 1
                info["started"] = info["started"] or obj.get("timestamp", "")
                info["cwd"] = info["cwd"] or obj.get("cwd", "")
                info["session_id"] = obj.get("sessionId") or info["session_id"]
            elif otype == "session_meta":
                info["tool"] = "codex"
                meta = obj.get("payload") or {}
                info["started"] = (info["started"] or obj.get("timestamp", "")
                                   or meta.get("timestamp", ""))
                info["cwd"] = info["cwd"] or meta.get("cwd", "") or obj.get("cwd", "")
                info["session_id"] = meta.get("id") or info["session_id"]
            elif otype == "response_item":
                info["tool"] = "codex"
                p = obj.get("payload")
                if isinstance(p, dict) and p.get("type") == "message":
                    info["messages"] += 1
                info["started"] = info["started"] or obj.get("timestamp", "")
    except Exception:
        pass
    return info


def claude_dir():
    return os.environ.get(
        "SESSION_HANDOVER_CLAUDE_DIR",
        os.path.expanduser("~/.claude/projects"))


def codex_dir():
    return os.environ.get(
        "SESSION_HANDOVER_CODEX_DIR",
        os.path.expanduser("~/.codex/sessions"))


def _iter_jsonl(root):
    if not os.path.isdir(root):
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def discover(fast=False, include_subagents=False):
    """Return list of session dicts, newest first. Never raises.

    fast=True uses peek() (cheap metadata scan) instead of full parsing --
    enough for `list`. Subagent (Task sidechain) transcripts are excluded
    unless include_subagents=True.
    """
    sessions = []
    for path in _iter_jsonl(claude_dir()):
        if not include_subagents and _is_subagent_transcript(path):
            continue
        try:
            sessions.append(peek(path) if fast else parse_claude_code(path))
        except Exception:
            continue
    for path in _iter_jsonl(codex_dir()):
        if not include_subagents and _is_subagent_transcript(path):
            continue
        try:
            sessions.append(peek(path) if fast else parse_codex(path))
        except Exception:
            continue

    def sort_key(s):
        try:
            return os.path.getmtime(s["path"])
        except OSError:
            return 0

    sessions.sort(key=sort_key, reverse=True)
    return sessions


def find_session(sessions, ident):
    """Match by full or prefix of session_id."""
    ident = ident.strip()
    for s in sessions:
        if s["session_id"] == ident or s["session_id"].startswith(ident):
            return s
    return None
