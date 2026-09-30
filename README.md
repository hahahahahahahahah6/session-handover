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

## Development

```bash
python3 tests/test_parsers.py
python3 tests/test_cli.py
```

## License

MIT
