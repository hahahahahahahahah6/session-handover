"""session-handover: hand off a coding-agent session to the next one.

Reads local session transcripts from Claude Code (~/.claude/projects) and
Codex CLI (~/.codex/sessions) and generates a structured Markdown handover.
"""

import argparse
import datetime
import os
import sys

from . import parsers
from . import compact_audit
from . import compaction_matrix
from . import precompact
from . import snapshot
from . import sessionend
from .handover import generate


def _fmt_time(s):
    started = s.get("started", "")
    if started:
        # ISO-ish timestamps: keep date + time, drop the rest
        return started[:16].replace("T", " ")
    try:
        ts = os.path.getmtime(s["path"])
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "?"


def cmd_list(args):
    sessions = parsers.discover(fast=True)
    if not sessions:
        print("No sessions found.")
        print("Looked in:")
        print("  " + parsers.claude_dir())
        print("  " + parsers.codex_dir())
        return 0
    print("%-12s %-24s %-17s %s" % ("TOOL", "SESSION", "STARTED", "MESSAGES"))
    for s in sessions[: args.limit]:
        print("%-12s %-24s %-17s %d" % (
            s["tool"], s["session_id"][:24], _fmt_time(s), s["messages"]))
    return 0


def cmd_make(args):
    sessions = parsers.discover()
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return 1
    if args.session:
        s = parsers.find_session(sessions, args.session)
        if not s:
            print("No session matching %r." % args.session, file=sys.stderr)
            return 1
    else:
        s = sessions[0]  # --last is the default
    text = generate(s)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("Wrote %s (%s / %s)" % (args.out, s["tool"], s["session_id"]))
    else:
        print(text)
    return 0


def cmd_audit_compact(args):
    sessions = parsers.discover()
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return 1
    if args.session:
        s = parsers.find_session(sessions, args.session)
        if not s:
            print("No session matching %r." % args.session, file=sys.stderr)
            return 1
    else:
        s = sessions[0]  # --last is the default
    result = compact_audit.audit(s["path"], s["tool"])
    text = compact_audit.render_terminal(result, s["session_id"])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(compact_audit.render_markdown(result,
                                                   s["session_id"]))
        print("Wrote %s (%s / %s)" % (args.out, s["tool"],
                                      s["session_id"]))
    else:
        print(text)
    if args.fail_on_drop and any(b["dropped"] for b in result["boundaries"]):
        return 1
    return 0


def cmd_hook_install(args):
    if args.which == "sessionend":
        command = sessionend.sessionend_install()
        print("Installed SessionEnd hook:")
        print("  %s" % command)
        print("Settings: %s" % sessionend.settings_path())
        return 0
    command = precompact.hook_install()
    print("Installed PreCompact hook:")
    print("  %s" % command)
    print("Settings: %s" % precompact.settings_path())
    return 0


def cmd_hook_uninstall(args):
    if args.which == "sessionend":
        removed = sessionend.sessionend_uninstall()
        if removed:
            print("Removed SessionEnd hook.")
        else:
            print("SessionEnd hook was not installed; nothing to do.")
        return 0
    removed = precompact.hook_uninstall()
    if removed:
        print("Removed PreCompact hook.")
    else:
        print("PreCompact hook was not installed; nothing to do.")
    return 0


def cmd_snapshot(args):
    snap = snapshot.take_snapshot(skills_dirs=args.skills_dir or None,
                                  claude_md=args.claude_md,
                                  memory_md=args.memory)
    if args.out:
        snapshot.save_snapshot(snap, args.out)
        print("Wrote %s (%d skill(s), CLAUDE.md %s, MEMORY.md %s)" % (
            args.out, len(snap["skills"]),
            "yes" if snap["claude_md"] else "no",
            "yes" if snap["memory_md"] else "no"))
    else:
        import json
        print(json.dumps(snap, indent=2, ensure_ascii=False))
    return 0


def _resolve_session(args):
    sessions = parsers.discover()
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return None
    if args.session:
        s = parsers.find_session(sessions, args.session)
        if not s:
            print("No session matching %r." % args.session, file=sys.stderr)
            return None
    else:
        s = sessions[0]  # --last is the default
    return s


def cmd_audit(args):
    try:
        snap = snapshot.load_snapshot(args.snapshot)
    except Exception as e:
        print("Cannot load snapshot: %s" % e, file=sys.stderr)
        return 2
    summary_texts = None
    if args.session or args.with_summary:
        s = _resolve_session(args)
        if s is None:
            return 1
        tool = {"claude-code": "claude-code", "codex": "codex"}.get(
            s["tool"], "claude-code")
        summary_texts = snapshot.latest_summaries(s["path"], tool)
    result = snapshot.audit_against_snapshot(
        snap, skills_dirs=args.skills_dir or None,
        claude_md=args.claude_md, memory_md=args.memory,
        summary_texts=summary_texts, snapshot_path=args.snapshot)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(snapshot.render_markdown(result))
        print("Wrote %s (%d problem(s))" % (args.out,
                                            len(result["problems"])))
    else:
        print(snapshot.render_terminal(result))
    if args.fail_on_problem and result["problems"]:
        return 1
    return 0


def cmd_hook(args):
    if not args.session_end:
        print("session-handover hook: nothing to do; pass --session-end.",
              file=sys.stderr)
        return 2
    return sessionend.sessionend_main(out=args.out, fmt=args.format)


def cmd_compact_matrix(args):
    if args.harness:
        try:
            h = compaction_matrix.get(args.harness)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        lines = ["# %s -- compaction semantics" % h["name"], "",
                 "- **Trigger:** %s" % h["trigger"], "",
                 "- **Mechanism:** %s" % h["mechanism"], "",
                 "- **Survives:** %s" % "; ".join(h["survives"]), "",
                 "- **Drops:** %s" % "; ".join(h["drops"]), "",
                 "- **Durable anchor:** %s" % h["anchor"], "",
                 "- **Transcript audit:** %s" % h["audit"],
                 "- **Confidence:** %s" % h["confidence"],
                 "- **Evidence:** %s" % h["evidence"], ""]
        print("\n".join(lines))
        return 0
    compare_pair = None
    if args.compare:
        if len(args.compare) != 2:
            print("--compare takes exactly two harness ids, e.g. "
                  "--compare claude-code codex-cli", file=sys.stderr)
            return 2
        try:
            compare_pair = tuple(args.compare)
            compaction_matrix.get(compare_pair[0])
            compaction_matrix.get(compare_pair[1])
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(compaction_matrix.render_markdown(
                compare_pair=compare_pair))
        print("Wrote %s (%d harnesses)" % (
            args.out, len(compaction_matrix.harnesses())))
    else:
        print(compaction_matrix.render_terminal())
        if compare_pair:
            c = compaction_matrix.compare(*compare_pair)
            a_name = compaction_matrix.get(compare_pair[0])["name"]
            b_name = compaction_matrix.get(compare_pair[1])["name"]
            print("")
            print("drops only in %s:" % a_name)
            for d in c["drops_only_in_a"] or ["(none -- same drop profile)"]:
                print("  - %s" % d)
            print("drops only in %s:" % b_name)
            for d in c["drops_only_in_b"] or ["(none -- same drop profile)"]:
                print("  - %s" % d)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="session-handover",
        description="Generate handover docs from Claude Code / Codex CLI session transcripts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="List recent local sessions.")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    pm = sub.add_parser("make", help="Generate a handover document.")
    pm.add_argument("--last", action="store_true",
                    help="Use the most recent session (default).")
    pm.add_argument("--session", metavar="ID",
                    help="Use the session whose id matches (prefix ok).")
    pm.add_argument("--out", metavar="HANDOVER.md",
                    help="Write to file instead of stdout.")
    pm.set_defaults(func=cmd_make)

    pa = sub.add_parser("audit-compact",
                        help="Audit what /compact dropped: extract durable "
                             "items from pre-compact turns and check them "
                             "against the compaction summary.")
    pa.add_argument("--last", action="store_true",
                    help="Use the most recent session (default).")
    pa.add_argument("--session", metavar="ID",
                    help="Use the session whose id matches (prefix ok).")
    pa.add_argument("--out", metavar="AUDIT.md",
                    help="Write the Markdown report to file instead of "
                         "printing the terminal summary.")
    pa.add_argument("--fail-on-drop", action="store_true",
                    help="Exit 1 when any item was dropped (for CI/hook "
                         "use). Default is exit 0: audit, never blocks.")
    pa.set_defaults(func=cmd_audit_compact)

    pi = sub.add_parser("hook-install",
                        help="Install a hook: auto-write a handover before "
                             "/compact (precompact), or a lightweight handoff "
                             "when a session ends (sessionend). Merges into "
                             "~/.claude/settings.json; idempotent.")
    pi.add_argument("which", nargs="?", default="precompact",
                    choices=["precompact", "sessionend"],
                    help="Which hook to install.")
    pi.set_defaults(func=cmd_hook_install)

    pu = sub.add_parser("hook-uninstall",
                        help="Remove a hook installed by hook-install. "
                             "Idempotent.")
    pu.add_argument("which", nargs="?", default="precompact",
                    choices=["precompact", "sessionend"],
                    help="Which hook to uninstall.")
    pu.set_defaults(func=cmd_hook_uninstall)

    ps = sub.add_parser("snapshot",
                        help="Freeze pre-compaction state: skill bodies "
                             "(~/.claude/skills or project skills/), "
                             "CLAUDE.md, MEMORY.md. Take this BEFORE "
                             "/compact.")
    ps.add_argument("--skills-dir", action="append", metavar="D",
                    help="Skill directory to scan (repeatable). Default: "
                         "probe ~/.claude/skills, ./skills, "
                         "./.claude/skills.")
    ps.add_argument("--claude-md", metavar="PATH",
                    help="Explicit CLAUDE.md path. Default: probe "
                         "./CLAUDE.md, ~/.claude/CLAUDE.md, ~/CLAUDE.md.")
    ps.add_argument("--memory", metavar="PATH",
                    help="Explicit MEMORY.md path. Default: probe "
                         "./MEMORY.md, ~/.claude/MEMORY.md, ~/MEMORY.md.")
    ps.add_argument("-o", "--out", metavar="SNAP.json",
                    help="Write the snapshot JSON to a file. Without -o, "
                         "prints it to stdout.")
    ps.set_defaults(func=cmd_snapshot)

    pa2 = sub.add_parser("audit",
                         help="After compaction: compare the current disk "
                              "against a pre-compaction snapshot and report "
                              "missing skill bodies, stale re-attaches, and "
                              "CLAUDE.md new-text loss.")
    pa2.add_argument("--snapshot", metavar="SNAP.json", required=True,
                     help="Snapshot file written by `snapshot`.")
    pa2.add_argument("--skills-dir", action="append", metavar="D",
                     help="Override skill directories (default: the ones "
                          "recorded in the snapshot).")
    pa2.add_argument("--claude-md", metavar="PATH",
                     help="Override CLAUDE.md path.")
    pa2.add_argument("--memory", metavar="PATH",
                     help="Override MEMORY.md path.")
    pa2.add_argument("--session", metavar="ID",
                     help="Cross-check CLAUDE.md new text against the "
                          "compaction summary of this session (prefix ok; "
                          "default: most recent session).")
    pa2.add_argument("--with-summary", action="store_true",
                     help="Same as --session for the most recent session.")
    pa2.add_argument("--out", metavar="AUDIT.md",
                     help="Write the Markdown report to file instead of "
                          "printing the terminal summary.")
    pa2.add_argument("--fail-on-problem", action="store_true",
                     help="Exit 1 when any problem is found (for CI/hook "
                          "use). Default is exit 0: audit, never blocks.")
    pa2.set_defaults(func=cmd_audit)

    ph = sub.add_parser("hook",
                        help="Run a hook body directly (used by Claude Code "
                             "hooks).")
    ph.add_argument("--session-end", action="store_true",
                    help="SessionEnd hook: write a lightweight handoff "
                         "(unfinished tasks, key decisions, follow-ups). "
                         "Reads hook JSON from stdin.")
    ph.add_argument("--format", choices=["json", "markdown"], default="json",
                    help="Handoff file format (default: json).")
    ph.add_argument("--out", metavar="PATH",
                    help="Write to this file (or directory) instead of the "
                         "default ~/.cache/session-handover/sessionend/.")
    ph.set_defaults(func=cmd_hook)

    pm2 = sub.add_parser("compact-matrix",
                         help="Cross-harness compaction semantics: what each "
                              "coding harness drops and keeps when it "
                              "compacts, and where to pin durable state.")
    pm2.add_argument("--out", metavar="MATRIX.md",
                     help="Write the full Markdown report to file instead "
                          "of printing the terminal table.")
    pm2.add_argument("--compare", nargs=2, metavar=("HARNESS_A", "HARNESS_B"),
                     help="Diff two harnesses' drop/survive profiles, e.g. "
                          "--compare claude-code codex-cli.")
    pm2.add_argument("--harness", metavar="ID",
                     help="Show one harness's full semantics, e.g. "
                          "--harness codex-cli.")
    pm2.set_defaults(func=cmd_compact_matrix)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
