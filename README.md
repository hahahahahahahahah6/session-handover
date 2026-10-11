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

## PreCompact auto-handover

v0.2 audits what compaction dropped. v0.3 prevents the loss: a PreCompact
hook writes the handover **before** the summary replaces context. The pain
is well documented — Silta's hand-written handoff+compaction process,
Recall's pre-compact hook, and the plan-mode Ask HN thread where people
manually split `PLAN_*.md` files all show the same thing: when `/compact`
fires, the session's durable state needs a structured handover written
*before* compaction, and today it's manual.

One command installs it:

```bash
$ session-handover hook-install precompact
Installed PreCompact hook:
  /home/you/.venv/bin/python /home/you/.cache/session-handover/hooks/precompact.py
Settings: /home/you/.claude/settings.json
```

The registered command uses the absolute path of the interpreter that ran
`hook-install` — never a bare `python3` — so the hook keeps working under
pipx, venv, and uv installs, where the system `python3` has no
`session_handover` package.

This merges a `PreCompact` entry into `~/.claude/settings.json` without
touching your other hooks (idempotent — re-running never duplicates it;
`hook-uninstall precompact` removes it). On every `/compact`, the hook:

1. Generates the standard handover (goal, timeline, files touched, errors,
   open items) from the transcript **as it currently stands**.
2. Appends a **"Durable items the next session must preserve"** checklist —
   rules, TODOs, decisions, preferences extracted from the recent turns,
   each citing its source turn — so even if the auto-summary is lossy, the
   restore checklist is on disk.
3. Writes it to `~/.cache/session-handover/precompact/HANDOVER.<session>.md`
   (override the directory with `SESSION_HANDOVER_DIR`).

The hook is **fail-open**: any failure — garbage stdin, missing transcript,
unparseable format — prints a stderr note and exits 0. It never blocks
compaction. Extraction is capped to the most recent 300 text turns
(`SESSION_HANDOVER_WINDOW` to tune) so the hook stays fast on large
transcripts.

## Compaction state snapshots

v0.3 prevents loss at `/compact` time. v0.4 audits what compaction did to
your **files**, not just your transcript. Observed failure modes (8
compaction runs against a real skill stack): 2/8 runs dropped a skill
body that was in context before `/compact`; re-attached skill bodies came
back as the **old** version even though the SKILL.md on disk had been
edited mid-session; editing CLAUDE.md mid-session and then compacting
left the summary carrying the old text and the new text gone from context.

Before compacting, freeze the state:

```bash
$ session-handover snapshot -o /tmp/pre-compact.json
Wrote /tmp/pre-compact.json (3 skill(s), CLAUDE.md yes, MEMORY.md no)
```

This records the full body + sha256 of every skill found in
`~/.claude/skills` (or `./skills`, `./.claude/skills`), plus CLAUDE.md
and MEMORY.md (paths probe defaults; override with `--skills-dir`,
`--claude-md`, `--memory`).

After compaction, audit the disk against the snapshot:

```bash
$ session-handover audit --snapshot /tmp/pre-compact.json
session-handover snapshot audit
snapshot taken: 2026-10-11T07:40:00+00:00
problems: 1

[STALE RE-ATTACH] /home/you/.claude/skills/deploy/SKILL.md
  Skill 'deploy' was modified after the snapshot (mtime 2026-10-11
  00:42:10) but its current content is byte-identical to the snapshot --
  an older version appears to have been re-attached over mid-session edits.
  fix: Re-apply any mid-session edits you made to
  /home/you/.claude/skills/deploy/SKILL.md. ...
```

Three mechanical problem classes:

- **Missing skill body** — in the snapshot, gone (or emptied) on disk
  now. The full pre-compaction body is stored in the snapshot file, so
  the suggested fix tells you exactly where to restore it from.
- **Stale re-attach** — the file was modified after the snapshot but its
  current content is byte-identical to the snapshot: an older version was
  re-attached over your mid-session edits. Re-apply them (the snapshot
  only holds the old version, so check your editor history).
- **CLAUDE.md new text lost** — CLAUDE.md gained lines after the
  snapshot. The audit shows the added lines (unified diff included in
  `--out` Markdown) and tells you to verify them against the resumed
  context. Pass `--session <id>` (or `--with-summary`) to cross-check the
  added lines against the actual compaction summary: if the summary
  doesn't contain them, the verdict upgrades from "verify" to "lost —
  paste these back".

`--out AUDIT.md` writes the full Markdown report; `--fail-on-problem`
exits 1 when anything is found (for CI/hook use). Like `audit-compact`,
the default is exit 0 — an audit never blocks.

## SessionEnd auto-handoff

`session-handover hook --session-end` is the SessionEnd counterpart to
the PreCompact hook: when a session ends, it extracts unfinished tasks,
key decisions, and follow-ups from the transcript tail and writes a
lightweight handoff file (`~/.cache/session-handover/sessionend/`,
JSON by default, `--format markdown` available). Fail-open, exits 0
always. Run it from a hook, or by hand:

```bash
$ echo '{"session_id":"abc","transcript_path":"/path/to/transcript.jsonl"}' \
  | session-handover hook --session-end --format markdown
```

Install it into Claude Code with one command (idempotent; merges into
`~/.claude/settings.json` without touching other hooks):

```bash
$ session-handover hook-install sessionend
```

or wire it manually in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/python /path/to/hooks/sessionend.py"
          }
        ]
      }
    ]
  }
}
```

## Cross-harness compaction matrix

v0.5 widens the audit from Claude Code to every harness a session might
be handed to. `compact-matrix` prints what each mainstream coding harness
drops and keeps when it compacts — trigger, mechanism, survivors, drops,
and the durable anchor where state should be pinned before it happens:

```bash
$ session-handover compact-matrix
HARNESS       TRIGGER                                      MECHANISM                AUDIT
claude-code   manual /compact; auto-compact near the...    Model-written summary... full
codex-cli     manual /compact; automatic pre-turn and...   Remote v2 compaction...  partial
gemini-cli    manual /compress; automatic under context... Model-written summary... none
...
```

Highlights from the 8-harness registry (entries marked *measured* were
counted in repeated runs; the rest are documented or community-reported):

- **Claude Code** — project-root CLAUDE.md re-injected from disk (8/8
  measured compactions); path-scoped rules and nested CLAUDE.md lost until
  a matching file is read again; invoked skill bodies re-attached 6/8,
  with stale text after an edit (rulestack).
- **Codex CLI** — remote v2 compaction returns an encrypted blob plus
  retained user messages; the local fallback drops all assistant messages
  and the summary becomes a "CONTEXT CHECKPOINT" user message. Reasoning
  items are filtered before summarization; the compaction prompt is
  configurable since v0.50.
- Gemini CLI (`/compress`), Aider, OpenCode, Copilot CLI (`/compact`,
  auto at ~95%), Cursor, Cline — documented with their durable anchors
  (GEMINI.md, committed files, AGENTS.md, copilot-instructions.md,
  `.cursor/rules`, `.clinerules`).

The full report — `session-handover compact-matrix --out MATRIX.md` —
includes a comparison table, per-harness detail, and evidence notes.
Pairwise diff: `--compare claude-code codex-cli`. Single harness:
`--harness codex-cli`.

The transcript audit engine is shared: `audit-compact` now also detects
Codex's real compaction checkpoints (remote v1 `**CONTEXT CHECKPOINT**`
assistant messages, the local fallback "another language model started
to solve..." prefix, `type=compaction` response items) instead of only
bare marker strings. A lone `/compact` command line is the trigger, not
the replacement — it no longer fabricates a boundary by itself.

## How it differs

- **vs session-hygiene's handoff card** — a session-hygiene Mod that
  carries state between sessions on the same machine. It does
  session-to-session handoff only; it doesn't audit what `/compact`
  dropped, and it has nothing to say about skill bodies or CLAUDE.md
  edits. session-handover owns the **compaction diff** boundary instead.
- **vs SpecWeave 3** — a heavy spec-first workflow (11 skills, JIRA
  sync). session-handover stays a **lightweight stdlib-only CLI**:
  no workflow to adopt, no services to sync, just `snapshot` /
  `audit` / hooks you can run from any shell.
- **Cross-tool, not Claude-only.** The audit engine started on Claude Code
  transcripts, but the author bounces between tools — so v0.5 adds a
  documented compaction-semantics matrix for 8 harnesses plus real Codex
  `/compact` checkpoint detection. The handover survives the tool switch,
  not just the compaction.

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
- **The PreCompact hook protocol is undocumented.** The `session_id` /
  `transcript_path` JSON shape on stdin comes from community documentation
  of Claude Code's hook protocol, not a stable API. If the protocol changes,
  the hook degrades to a no-op (fail-open). The pre-compaction checklist
  inherits the audit's extraction limits — it is a heuristic safety net,
  not a guarantee that nothing was lost.

- **The snapshot audit is mechanical, not semantic.** It compares the disk
  against the snapshot; it cannot see the resumed context itself, so a
  "lost" verdict about context should be confirmed by skimming the
  session. The stale-reattach check needs the file's mtime to be clearly
  newer than the snapshot (2s margin) — a same-second rewrite won't flag.
  Skill bodies are stored verbatim in the snapshot JSON, so keep snapshot
  files somewhere sensible and don't commit ones containing secrets.
- **The SessionEnd handoff is a recall floor.** Buckets (unfinished /
  decisions / follow-ups) come from the same heuristic patterns as the
  compaction audit — subtly-phrased items are missed, and a rule is filed
  under "follow-ups & constraints", not magically understood.

## Development

```bash
python3 tests/test_parsers.py
python3 tests/test_cli.py
python3 tests/test_compact_audit.py
python3 tests/test_compact_matrix.py
python3 tests/test_precompact.py
python3 tests/test_snapshot.py
python3 tests/test_sessionend.py
```

## Changelog

### v0.5.0
- **Cross-harness compaction matrix: `compact-matrix`.** A documented
  registry of 8 harnesses' compaction semantics (trigger, mechanism,
  survives, drops, durable anchor, transcript-audit level), rendered as a
  terminal table or full Markdown report (`--out MATRIX.md`), with
  pairwise diffs (`--compare claude-code codex-cli`) and per-harness
  detail (`--harness codex-cli`). Claude Code's entry is grounded in
  rulestack's 8 measured compactions (root CLAUDE.md 8/8, skill body 6/8
  with stale text after edit); Codex CLI's in the remote-v2 / local-
  fallback behavior documented by the community.
- **Codex `/compact` audit upgraded from marker strings to checkpoint
  detection.** `audit-compact` now recognizes Codex's real checkpoint
  shapes (remote v1 `**CONTEXT CHECKPOINT**` assistant message, local
  fallback "another language model started to solve..." user message,
  `type=compaction` response items). A bare `/compact` command line is
  now treated as the trigger, not the replacement summary — it no longer
  fabricates a boundary.

### v0.4.0
- **Compaction state snapshots: `snapshot` + `audit`.** `snapshot -o
  SNAP.json` freezes skill bodies (hash + full content), CLAUDE.md, and
  MEMORY.md before `/compact`; `audit --snapshot SNAP.json` compares the
  post-compact disk against it and reports three mechanical problems —
  missing skill body (restorable from the snapshot), stale re-attach (file
  modified after the snapshot but byte-identical to it), and CLAUDE.md
  new-text loss (added lines shown, optionally cross-checked against the
  compaction summary via `--session`). `--fail-on-problem` for CI/hook
  use; default exit 0, never blocks.
- **SessionEnd auto-handoff: `hook --session-end`.** Writes a lightweight
  handoff (unfinished tasks, key decisions, follow-ups; JSON or Markdown)
  when a session ends. Fail-open, exits 0. `hook-install sessionend` /
  `hook-uninstall sessionend` register it in `~/.claude/settings.json`;
  the README shows the manual hook config too.
- New "How it differs" section: vs session-hygiene's handoff card
  (session-to-session Mod, no compaction audit) and vs SpecWeave 3 (heavy
  spec-first workflow) — this tool stays a lightweight stdlib-only CLI on
  the compaction-diff boundary.

### v0.3.1
- **Fixed: `hook-install` no longer hardcodes `python3`.** The registered
  hook command now uses the absolute path of the interpreter that ran
  `hook-install` (`sys.executable`). Previously, pipx/venv/uv installs got
  a `python3 <shim>` command whose interpreter had no `session_handover`
  package, so the PreCompact hook fail-opened on every `/compact` while
  the user believed they were protected.
- **Fixed: session titles are no longer treated as compaction boundaries.**
  Bare `{"type": "summary"}` lines are the `/resume` session titles, not
  compactions; they only count as boundaries when the transcript also
  carries real `/compact` markers (a `system` entry with subtype
  `compact_boundary`, or a user message flagged `isCompactSummary`).
  Previously, any titled session made the audit report every earlier rule
  as DROPPED. The continuation-preamble path is unchanged.

### v0.3
- PreCompact auto-handover: `hook-install precompact` registers a
  fail-open PreCompact hook that writes
  `HANDOVER.<session>.md` (handover + durable-item checklist) before
  `/compact` runs.

### v0.2
- Compaction diff audit: `audit-compact` extracts durable items
  (rules, TODOs, decisions, preferences) from pre-compact turns and
  checks them against the compaction summary via token overlap.

## License

MIT
