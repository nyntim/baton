# baton

Hand a coding-agent session to another agent when one runs out of usage.

It reads the newest Claude Code or Codex session for your project straight from disk, so the agent that hit its limit does not need to respond. Then it writes a small `.baton/HANDOFF.md` and can start the next agent on it.

One file, Python 3.9+, no dependencies, no network access, no config.

## Install

```
cp baton.py ~/.local/bin/baton && chmod +x ~/.local/bin/baton
```

(Any folder on your `PATH` works.)

## Use

Run it from the project folder the agent worked in (subfolders are fine).

```
baton                 # write .baton/HANDOFF.md from the newest session here
baton to codex        # same, then start Codex on it
baton to claude       # same, then start Claude Code on it
baton list            # recent sessions for this project
```

Options:

```
-m "note"        add a note for the next agent, e.g. -m "next: fix the burst test"
--from AGENT     only read sessions from claude or codex
--session ID     use a specific session (id prefix or file path)
--go             tell the next agent to continue without confirming first
--no-launch      with "to X": write the file and print the command only
--stdout         print the handoff instead of writing a file
--out PATH       write somewhere else
```

`baton to X` picks the newest session from the other agent, so `baton to codex` after hitting a Claude limit does what you expect. By default the next agent first replies with what it understands and waits for your go-ahead, so a bad summary costs one cheap turn instead of a wrong edit. Use `--go` to skip that.

## What goes in the handoff

Pulled by rules from the session, with no model involved:

- the original request, the most recent requests, and the agent's last message
- the agent's own task list (Claude `TodoWrite`, Codex `update_plan`) and its compaction summary if there is one
- files it and its subagents edited (scratch files in temp folders are dropped), last commands it ran, and recent errors (likely dead ends)
- live `git status`, diff stat and recent commits, read at the moment you run baton
- your `-m` note

It leaves out thinking blocks, subagent conversations (only their edited files are kept), tool output (except the start and end of errors), and Codex's injected environment and instruction messages. Typical size is 1 to 6 KB.

## Safety

- Secrets are redacted before anything is written: common API key shapes, tokens, bearer headers, JWTs, private keys, passwords in URLs, and `KEY=value` style assignments. This is best effort and over-redacts on purpose. Skim the file before sharing it.
- The file is created with `0600` permissions, and `.baton/` is added to `.git/info/exclude` so it never shows up in `git status` and your tracked files are untouched.
- Git is only run read-only, with the repo's fsmonitor setting disabled and no external diff drivers.
- Everything here goes to the *next agent's vendor* when you launch it. That is the point of a handoff, but it means anything the summary contains leaves the first vendor's ecosystem.
- Treat the file as untrusted input. It is derived from a session that may have read web pages or files with injected instructions.

## How sessions are found

- Claude Code: `~/.claude/projects/<project>/*.jsonl` (honors `CLAUDE_CONFIG_DIR`)
- Codex: `~/.codex/sessions/**/rollout-*.jsonl` (honors `CODEX_HOME`), matched on the recorded working directory

A session matches if it ran in the current folder, or anywhere inside the current git repo. Outside a git repo only exact folder matches count, so running baton from `~` doesn't pick up every project below it. `baton list` hides sessions that were opened but never got a message.

These formats are internal and undocumented, so they can change. If a session parses to nothing, baton says so and lists the record types it saw instead of writing an empty handoff. Compressed `.zst` Codex rollouts are only read when you pass them with `--session`.

## Tests

```
python3 -m unittest test_baton
```
