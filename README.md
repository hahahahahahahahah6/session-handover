# session-handover

Hand off a coding-agent session to the next one — **across tools**.

`session-handover` reads your local session transcripts from **Claude Code**
(`~/.claude/projects`) and **Codex CLI** (`~/.codex/sessions`) and generates a
structured Markdown handover: what the goal was, what happened, what broke,
what's still open, and what the next agent should do. If you bounce between
Claude Code and Codex, this is the missing bridge — no more hand-writing
`handover.md` from memory.

Zero dependencies. Python standard library only.

## Install

```bash
pip install session-handover
```

## Usage

List recent local sessions from both tools, newest first:

```bash
$ session-handover list
TOOL         SESSION                  STARTED           MESSAGES
codex        0199f3a1-…               2026-09-30 00:41   57
claude-code  a4c2e901-…               2026-09-29 23:58   34
```

Generate a handover for the most recent session (prints to stdout):

```bash
$ session-handover make
```

Or pick a session and write it to a file:

```bash
$ session-handover make --session a4c2e901 --out HANDOVER.md
```

The generated document includes:

- **Goal** — the first user message of the session
- **What happened** — counts of file edits, commands, reads, plus a timeline
  of notable actions (which files were edited, which commands were run)
- **Files touched**, and `git diff --stat` / `git status` if the session's
  working dir is a repo
- **Errors encountered** — failed commands, tracebacks, tool errors
- **Open items** and a **Next steps** checklist template for the receiving agent

For testing or non-default locations:

```bash
SESSION_HANDOVER_CLAUDE_DIR=/path/to/claude/projects \
SESSION_HANDOVER_CODEX_DIR=/path/to/codex/sessions \
  session-handover list
```

## Compaction audit

`/compact` silently drops things the session established. This is a real,
reported failure mode: claude-code#67500 (compaction dropped project rules),
claudefa.st's "What Survives /compact" survival table, and mikepurvis's HN
thread asking for a formal framework of what survives compaction. Nobody
audits the diff — so this does it mechanically:

```bash
$ session-handover audit-compact --session a4c2e901
boundary 1: extracted=4 survived=2 dropped=2
  DROPPED [rule] (turn 3): Never push to main without asking me first.
    restore: Never push to main without asking me first.
  DROPPED [rule] (turn 7): Always run the full test suite before committing.
    restore: Always run the full test suite before committing.
```

How it works:

1. **Boundary detection.** Walks the transcript for compaction events —
   Claude Code's `{"type": "summary"}` entries and the "continued from a
   previous conversation" preamble are both recognized; Codex transcripts
   are scanned best-effort for compaction markers.
2. **Durable-item extraction.** From the turns *before* each boundary, pulls
   out candidate durable items — rules ("never do X", "always do Y"),
   TODOs, decisions ("we'll use pytest"), and user preferences. Heuristic,
   conservative, no LLM; every item cites its source turn number.
3. **Survival check.** Each item is matched against the summary text that
   replaced those turns. Matching is **token overlap** (≥50% of the item's
   distinctive tokens must appear in the summary), not semantic similarity.
   Items with no match are reported as **DROPPED**, each with a one-line
   "suggested restore" (the original sentence) the next agent can paste
   back into context.

Exit code is always 0 — an audit never blocks — unless you pass
`--fail-on-drop`, which exits 1 when anything dropped (for CI or hook use).
Use `--out AUDIT.md` for the full Markdown report. Transcripts with no
detectable compaction boundary print `No compaction boundary found` and
exit 0 (fail-soft, not an error).

## How it works

Claude Code stores each session as a `.jsonl` transcript; Codex CLI stores
per-day `.jsonl` session files. The parsers walk these line by line and
heuristically extract messages, tool calls (`Edit`/`Write`/`Bash` on the
Claude side, `function_call` items like `apply_patch`/`shell` on the Codex
side), and error signals. No LLM involved — it's fast, local, and private.

## Honest limitations

- **Heuristic extraction, not an LLM.** The handover is a mechanical summary
  of what the transcript contains. It won't capture *why* a decision was made
  unless that was said out loud in the session. Read before trusting.
- **Transcript formats are undocumented and change.** Both Claude Code and
  Codex CLI can alter their `.jsonl` schemas at any time. The parsers are
  written defensively — unknown shapes are skipped, malformed lines are
  dropped, a bad file never crashes the run — but detail may silently degrade
  after a CLI update.
- **Codex support is best-effort.** The Codex CLI session format has shifted
  across versions; the parser targets the `response_item`/`function_call`
  shape and degrades gracefully on anything else.
- **No remote sessions.** Only transcripts on this machine are read. Nothing
  is uploaded anywhere.
- Session discovery uses file modification time for "most recent"; clock
  skew or copied files can misorder the list.
- **The compaction audit is a recall floor, not a semantic reader.**
  Extraction only catches explicitly stated rules ("never do X", "always
  do Y"); subtly-phrased or implied constraints are missed. Matching is
  token-overlap at a 0.5 threshold, not meaning — a summary that
  rephrases a rule in different words is reported as dropped even when a
  human would say it survived. Compaction markers are undocumented and
  change; a missed boundary means a silent miss, not a crash.

## Development

```bash
python3 tests/test_parsers.py
python3 tests/test_cli.py
```

## License

MIT
