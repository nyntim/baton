# Handoff: baton

You are picking up a small CLI tool called `baton`. Your job is to keep testing it and improve it. Read this whole file before changing anything, then confirm your understanding with the user.

## What baton is for

The user (Tim) works with Claude Code and Codex and keeps hitting usage limits mid-task. Once an agent is out of usage he cannot talk to it, so it cannot write its own handoff. baton solves this by reading the exhausted agent's session file straight from disk, extracting what a fresh agent needs, and writing `.baton/HANDOFF.md`. It can then launch the other agent with a prompt pointing at that file.

Requirements Tim set, in order:

1. Must work when the first agent is fully locked out (no cooperation from it).
2. Must be light on the receiving agent's context (no transcript replay).
3. Must be secure. He asked for a security review of existing tools before deciding to build his own.
4. Full tool, not an MVP, but not complex, and simple to use.

## Why it was built instead of reused

Existing tools were audited by reading their source: TStansel/handoff (Rust), handoff-agent (PyPI, HacksonClark), agentswap (PyPI), and Agent Handoff Console (macOS app). All are tiny, young projects with almost no users and no outside reviews. None combined all of: local-only, small output, secret redaction, and no writes into the agents' own home directories. baton borrows the best ideas: agentswap's "transmit only what the repo can't tell you" and confirm-first prompt, and handoff-agent's redaction and 0600 files.

## Files

- `baton.py`: the whole tool. Single file, Python 3.9+ target, standard library only, no network.
- `test_baton.py`: 21 unittest tests with synthetic Claude Code and Codex fixtures. Run with `python3 -m unittest test_baton`.
- `README.md`: user-facing docs.
- `HANDOFF.md`: this file.

## Current state

- All 21 tests pass on Python 3.12, Linux.
- An end-to-end demo on a scratch git repo produced a correct 1.3 KB handoff with 0600 permissions and `.baton/` added to `.git/info/exclude`.
- One bug was found and fixed during testing: `baton list` printed unredacted secret prefixes in its preview line. A regression test covers it.

## How it works (map of baton.py)

- **CLI** (`main`, `_run`): `baton`, `baton to claude|codex`, `baton list`, plus `-m`, `--from`, `--session`, `--out`, `--stdout`, `--go`, `--no-launch`. `to X` reads the newest session from the *other* agent. Launch uses `os.execv` on POSIX and `subprocess.call` on Windows.
- **Discovery** (`claude_candidates`, `codex_candidates`, `find_session`): matches sessions to the current directory, its realpath, and the git root. Picks the newest file across agents that has at least one real user message, trying up to 8 files.
  - Claude: `$CLAUDE_CONFIG_DIR` or `~/.claude`, under `projects/<cwd with non-alphanumerics replaced by '-'>/*.jsonl`. Falls back to scanning every project folder for a matching recorded `cwd`.
  - Codex: `$CODEX_HOME` or `~/.codex`, under `sessions/**/rollout-*.jsonl`. Matches the `cwd` in the first lines; the newest 300 files are scanned. `.zst` files are skipped in discovery and only readable via `--session`.
- **Parsers** (`parse_claude`, `parse_codex`): normalize both formats into a `Session` with users, assistants, edited files, commands, errors, todos, and the compaction summary.
  - Claude: skips `isSidechain`, `isMeta`, thinking blocks, and slash-command/system-reminder noise. Reads `Edit`/`Write`/`MultiEdit`/`NotebookEdit` paths, `Bash` commands, the last `TodoWrite`, `tool_result` blocks with `is_error`, and `isCompactSummary` messages.
  - Codex: reads `session_meta` and `turn_context` for metadata and `response_item` payloads (plus older top-level items). Skips `event_msg` to avoid duplicate user messages, and filters injected `<environment_context>`/AGENTS.md messages. Shell commands come from `function_call` and `local_shell_call`, edited files from `apply_patch` headers, todos from `update_plan`, and errors from non-zero exit codes in outputs.
  - If a file yields no user messages, `_loaded` raises a clear "format may have changed" error that lists the record types it saw.
- **Rendering** (`render`): fixed sections with size limits set as constants at the top of the file. Every inserted string passes through `Redactor`. The git section uses live repo state at run time.
- **Redaction** (`Redactor`): regexes for common key shapes, bearer tokens, JWTs, PEM private keys, URL credentials, and `KEY=value` style assignments. It over-redacts on purpose.
- **Git** (`git`): read-only, with `--no-optional-locks`, `core.fsmonitor=false`, `--no-ext-diff`, and `GIT_TERMINAL_PROMPT=0`, so a hostile repo config can't run code.

## What is NOT verified yet (start here)

1. **Real session files.** Both parsers were written from memory of the formats and tested only against synthetic fixtures. This is the biggest risk. Run baton against real Claude Code and Codex sessions, and adjust the parsers and fixtures to match what is actually on disk. Check especially:
   - the Claude project folder naming (the `re.sub` encoding in `claude_candidates`)
   - current Codex record types and field names (`payload.type`, `arguments` vs `input`, output and exit code shape)
   - Codex injected-message prefixes in `_NOISE_PREFIXES`
   - where current Claude Code versions store subagent transcripts
2. **macOS.** Tim uses a MacBook Air M3, and it hasn't been run on macOS. Watch for `/tmp` vs `/private/tmp` realpath differences and launching `claude`/`codex` via `os.execv`.
3. **Python 3.9 to 3.11.** The 3.9+ target is claimed but only 3.12 was run.
4. **Windows.** The code path exists but is untested.

## Known limitations and ideas (only if Tim wants them)

- Redaction misses generic high-entropy strings and has false positives (for example `tokenizer: whitespace` gets redacted). Tighten only with tests on both sides.
- Old compressed Codex sessions need `--session`.
- No Cursor, Gemini CLI, or OpenCode support. Each would be a new parser plus discovery function.
- Rule-based extraction can't know the true next step. The `-m` note covers that today.

## Constraints from Tim (follow these)

- Never use em dashes, in code, comments, docs, or messages.
- Never add Claude or any AI attribution in commits, co-author trailers, or branch names.
- Keep scope to exactly what is asked. Don't add features he didn't request. Propose them instead.
- Ask for missing information rather than assuming.
- Casual and direct tone is fine, but keep work output clear and efficient.
- Keep the tool simple: one file, standard library only, no network, no config file. Don't regress the security choices above (redaction before writing, 0600 files, git exclude, hardened read-only git, confirm-first prompt by default).

## Suggested first steps

1. Unzip, run `python3 -m unittest test_baton`, and confirm all 21 tests pass in your environment.
2. Ask Tim to run `baton list` and `baton --stdout` in a real project that has both Claude Code and Codex sessions, or to share a sanitized session file.
3. Fix any parser mismatches, and add a fixture built from the real format for each one.
