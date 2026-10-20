#!/usr/bin/env python3
"""baton: hand a coding-agent session to another agent.

Reads the newest Claude Code or Codex session for the current project straight
from disk (the agent that ran out of usage does not need to respond), pulls out
what a fresh agent needs, and writes .baton/HANDOFF.md.

    baton               write the handoff from the newest session here
    baton to codex      write it, then start Codex on it
    baton to claude     write it, then start Claude Code on it
    baton list          show recent sessions for this project

No network access, no dependencies, no config. Secrets are redacted (best effort).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__version__ = "1.0.0"

AGENTS = ("claude", "codex")
LABELS = {"claude": "Claude Code", "codex": "Codex"}
OUT_DIR = ".baton"
OUT_FILE = "HANDOFF.md"

# Size limits keep the handoff small. Tune here.
MAX_REQUEST = 1500
MAX_RECENT_USER = 600
MAX_LAST_ASSISTANT = 1800
MAX_COMPACT = 2500
RECENT_USER_COUNT = 3
MAX_FILES = 25
MAX_COMMANDS = 10
MAX_COMMAND_CHARS = 200
MAX_ERRORS = 5
MAX_ERROR_CHARS = 300
MAX_TODOS = 15
MAX_GIT_LINES = 30
CODEX_SCAN_LIMIT = 300  # newest Codex rollouts inspected when matching a project

CONFIRM = (
    "First reply with 3-4 sentences on what you understand the task and the current "
    "state to be and what you plan to do next, then wait for my go-ahead before "
    "changing anything."
)
GO = "Verify the repo state first, then continue from the next step."


class BatonError(Exception):
    """A problem worth showing to the user without a traceback."""


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_PLAIN_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]
# user:password@host inside URLs
_URL_CREDS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)")
# NAME_KEY=value, "password": "value", token: value
_ASSIGN = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:key|secret|token|password|passwd|credential)[A-Za-z0-9_]*)"
    r"(\"?\s*[=:]\s*)([\"']?)[^\s\"',;]{6,}"
)
MARK = "[REDACTED]"


class Redactor:
    """Best-effort secret scrubber. It over-redacts on purpose."""

    def __init__(self) -> None:
        self.count = 0

    def clean(self, text: str) -> str:
        if not text:
            return text
        for rx in _PLAIN_PATTERNS:
            text = rx.sub(self._hit, text)
        text = _URL_CREDS.sub(lambda m: self._tally(m.group(1) + MARK + m.group(2)), text)
        text = _ASSIGN.sub(self._assign, text)
        return text

    def _tally(self, replacement: str) -> str:
        self.count += 1
        return replacement

    def _hit(self, _m: re.Match) -> str:
        return self._tally(MARK)

    def _assign(self, m: re.Match) -> str:
        if MARK in m.group(0):
            return m.group(0)
        return self._tally(f"{m.group(1)}{m.group(2)}{m.group(3)}{MARK}")


# --------------------------------------------------------------------------- #
# Session model
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    agent: str
    path: Path
    session_id: str = ""
    cwd: str = ""
    branch: str = ""
    started: str = ""
    ended: str = ""
    users: list = field(default_factory=list)
    assistants: list = field(default_factory=list)
    files: dict = field(default_factory=dict)  # path -> touches, ordered by last touch
    commands: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    todos: list = field(default_factory=list)  # (status, text)
    compact: str = ""
    skipped: Counter = field(default_factory=Counter)
    records: int = 0
    bad_lines: int = 0

    def touch(self, path: str) -> None:
        path = (path or "").strip()
        if path:
            self.files[path] = self.files.pop(path, 0) + 1

    def note_meta(self, ts, cwd=None, branch=None) -> None:
        if isinstance(ts, str) and ts:
            self.started = self.started or ts
            self.ended = ts
        if cwd and not self.cwd:
            self.cwd = str(cwd)
        if branch:
            self.branch = str(branch)

    @property
    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    @property
    def message_count(self) -> int:
        return len(self.users) + len(self.assistants)


# --------------------------------------------------------------------------- #
# Reading files
# --------------------------------------------------------------------------- #

def _zst_bytes(path: Path) -> bytes:
    try:  # Python 3.14+
        from compression import zstd  # type: ignore

        return zstd.decompress(path.read_bytes())
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore

        with open(path, "rb") as fh:
            return zstandard.ZstdDecompressor().stream_reader(fh).read()
    except ImportError:
        pass
    if shutil.which("zstd"):
        r = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, timeout=60)
        if r.returncode == 0:
            return r.stdout
    raise BatonError(f"{path.name} is zstd-compressed; install the 'zstd' tool or 'zstandard' to read it.")


def _lines(path: Path):
    if path.suffix == ".zst":
        for raw in _zst_bytes(path).splitlines():
            yield raw.decode("utf-8", "replace")
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            yield from fh


def _records(path: Path, s: Session):
    for line in _lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            s.bad_lines += 1
            continue
        if isinstance(obj, dict):
            s.records += 1
            yield obj
        else:
            s.bad_lines += 1


def _blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({"type": "text", "text": item})
        return out
    return []


def _text_of(content) -> str:
    return "\n".join(
        str(b.get("text", "")) for b in _blocks(content) if b.get("type") in (None, "text", "input_text", "output_text")
    ).strip()


_NOISE_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>", "<local-command",
    "<system-reminder>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "caveat: the messages below", "[request interrupted",
    "<environment_context>", "<user_instructions>", "<instructions>", "<turn_aborted>",
    "# agents.md instructions", "<permissions instructions>", "<user_action>",
)


def _is_noise(text: str) -> bool:
    t = text.lstrip().lower()
    return not t or t.startswith(_NOISE_PREFIXES)


# --------------------------------------------------------------------------- #
# Claude Code parser
# --------------------------------------------------------------------------- #

def parse_claude(path: Path) -> Session:
    s = Session(agent="claude", path=path, session_id=path.stem)
    for obj in _records(path, s):
        if obj.get("isSidechain"):
            continue  # subagent traffic
        t = obj.get("type")
        s.note_meta(obj.get("timestamp"), obj.get("cwd"), obj.get("gitBranch"))
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")

        if t == "user":
            if obj.get("isCompactSummary"):
                s.compact = _text_of(content)
                continue
            if obj.get("isMeta"):
                continue
            for b in _blocks(content):
                bt = b.get("type")
                if bt == "text":
                    txt = str(b.get("text", "")).strip()
                    if not _is_noise(txt):
                        s.users.append(txt)
                elif bt == "tool_result" and b.get("is_error"):
                    err = _text_of(b.get("content"))
                    if err and not err.startswith("The user doesn't want"):
                        s.errors.append(err)
        elif t == "assistant":
            for b in _blocks(content):
                bt = b.get("type")
                if bt == "text" and str(b.get("text", "")).strip():
                    s.assistants.append(str(b["text"]).strip())
                elif bt == "tool_use":
                    _claude_tool(s, str(b.get("name", "")), b.get("input") or {})
        else:
            s.skipped[str(t)] += 1
    return s


def _claude_tool(s: Session, name: str, inp: dict) -> None:
    if not isinstance(inp, dict):
        return
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        s.touch(inp.get("file_path") or inp.get("notebook_path") or "")
    elif name == "Bash":
        cmd = str(inp.get("command", "")).strip()
        if cmd:
            s.commands.append(cmd)
    elif name == "TodoWrite":
        todos = inp.get("todos")
        if isinstance(todos, list):
            s.todos = [
                (str(t.get("status", "")), str(t.get("content") or t.get("activeForm") or ""))
                for t in todos
                if isinstance(t, dict)
            ]


# --------------------------------------------------------------------------- #
# Codex parser
# --------------------------------------------------------------------------- #

_CODEX_ITEMS = {
    "message", "function_call", "function_call_output", "custom_tool_call",
    "custom_tool_call_output", "local_shell_call", "reasoning",
}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$", re.M)
_EXIT_RES = (re.compile(r"Process exited with code (-?\d+)"), re.compile(r"[Ee]xit code:? (-?\d+)"))


def _codex_id(path: Path) -> str:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.name)
    return m.group(1) if m else path.stem


def parse_codex(path: Path) -> Session:
    s = Session(agent="codex", path=path, session_id=_codex_id(path))
    for obj in _records(path, s):
        t = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else None

        if t in ("session_meta", "turn_context") or (t is None and obj.get("id")):
            p = payload or obj
            git = p.get("git") if isinstance(p.get("git"), dict) else {}
            s.note_meta(obj.get("timestamp") or p.get("timestamp"), p.get("cwd"), git.get("branch"))
            if t != "turn_context" and p.get("id"):
                s.session_id = str(p["id"])
            continue

        if t == "response_item" and payload:
            rec = payload
        elif t in _CODEX_ITEMS:
            rec = obj  # older rollouts store items at the top level
        else:
            s.skipped[str(t)] += 1
            continue
        s.note_meta(obj.get("timestamp"))
        _codex_item(s, rec)
    return s


def _json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except ValueError:
            return {}
    return {}


def _codex_cmd(args: dict) -> str:
    cmd = args.get("command") or args.get("cmd") or ""
    if isinstance(cmd, list):
        parts = [str(c) for c in cmd]
        if len(parts) >= 3 and Path(parts[0]).name in ("bash", "sh", "zsh") and parts[1] in ("-lc", "-c"):
            return parts[2]
        return " ".join(parts)
    return str(cmd)


def _codex_item(s: Session, rec: dict) -> None:
    kind = rec.get("type")
    if kind == "message":
        text = _text_of(rec.get("content"))
        role = rec.get("role")
        if role == "user" and not _is_noise(text):
            s.users.append(text)
        elif role == "assistant" and text:
            s.assistants.append(text)
        return

    if kind in ("function_call", "custom_tool_call", "local_shell_call"):
        name = str(rec.get("name") or "")
        if kind == "local_shell_call":
            raw = rec.get("action") or {}
        elif kind == "function_call":
            raw = rec.get("arguments")
        else:
            raw = rec.get("input")
        args = _json_obj(raw)
        if name == "update_plan":
            plan = args.get("plan")
            if isinstance(plan, list):
                s.todos = [
                    (str(p.get("status", "")), str(p.get("step", ""))) for p in plan if isinstance(p, dict)
                ]
            return
        if args:
            text = _codex_cmd(args) or str(args.get("input") or args.get("patch") or "")
        else:
            text = raw if isinstance(raw, str) else ""
        patched = _PATCH_FILE.findall(text) + _PATCH_MOVE.findall(text)
        if name == "apply_patch" or patched:
            for p in patched:
                s.touch(p)
        elif text.strip():
            s.commands.append(text.strip())
        return

    if kind in ("function_call_output", "custom_tool_call_output"):
        text, code = _codex_output(rec)
        if code not in (None, 0) and text.strip():
            s.errors.append(text.strip())
        return

    s.skipped[str(kind)] += 1


def _codex_output(rec: dict):
    out = rec.get("output")
    text, code = "", None
    if isinstance(out, dict):
        text = str(out.get("content") or out.get("output") or "")
    elif isinstance(out, str):
        text = out
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                text = str(obj.get("output", text))
                meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
                code = meta.get("exit_code")
        except ValueError:
            pass
    if code is None:
        for rx in _EXIT_RES:
            m = rx.search(text)
            if m:
                code = int(m.group(1))
                break
    return text, code


PARSERS = {"claude": parse_claude, "codex": parse_codex}


# --------------------------------------------------------------------------- #
# Finding sessions
# --------------------------------------------------------------------------- #

def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _real(p: str) -> str:
    return os.path.realpath(p)


def _claude_base() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _codex_base() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def _first_cwd(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                p = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
                if p.get("cwd"):
                    return str(p["cwd"])
    except OSError:
        pass
    return ""


def _cwd_match(session_cwd: str, cwds: list) -> bool:
    if not session_cwd:
        return False
    sc = _real(session_cwd)
    for c in cwds:
        if sc == c or c in Path(sc).parents:
            return True
    return False


def claude_candidates(cwds: list) -> list:
    base = _claude_base()
    if not base.is_dir():
        return []
    wanted = {re.sub(r"[^A-Za-z0-9]", "-", c) for c in cwds}
    out = []
    for d in base.iterdir():
        if d.is_dir() and d.name in wanted:
            out.extend(d.glob("*.jsonl"))
    if not out:  # the folder naming can drift between versions: match on the recorded cwd instead
        for d in base.iterdir():
            if not d.is_dir():
                continue
            newest = sorted(d.glob("*.jsonl"), key=_mtime, reverse=True)[:1]
            if newest and _cwd_match(_first_cwd(newest[0]), cwds):
                out.extend(d.glob("*.jsonl"))
    return out


def codex_candidates(cwds: list) -> list:
    # Compressed (.zst) rollouts are old and expensive to scan; only --session reads them.
    base = _codex_base()
    if not base.is_dir():
        return []
    files = sorted(base.rglob("rollout-*.jsonl"), key=_mtime, reverse=True)[:CODEX_SCAN_LIMIT]
    return [f for f in files if _cwd_match(_first_cwd(f), cwds)]


CANDIDATES = {"claude": claude_candidates, "codex": codex_candidates}


def find_session(agents, cwds: list):
    pool = []
    for a in agents:
        for f in CANDIDATES[a](cwds):
            pool.append((_mtime(f), a, f))
    pool.sort(key=lambda x: x[0], reverse=True)
    for _, a, f in pool[:8]:
        s = PARSERS[a](f)
        if s.users:
            return s
    return None


def load_explicit(ref: str) -> Session:
    p = Path(ref).expanduser()
    if p.is_file():
        agent = "codex" if p.name.startswith("rollout-") else "claude"
        return _loaded(PARSERS[agent](p))
    ref = ref.strip()
    if len(ref) < 4 or not re.fullmatch(r"[0-9a-fA-F-]+", ref):
        raise BatonError(f"'{ref}' is neither a file nor a session id prefix.")
    hits = []
    cb, xb = _claude_base(), _codex_base()
    if cb.is_dir():
        hits += [("claude", f) for f in cb.glob(f"*/{ref}*.jsonl")]
    if xb.is_dir():
        hits += [("codex", f) for f in xb.rglob(f"rollout-*{ref}*.jsonl*")]
    if not hits:
        raise BatonError(f"No session matches '{ref}'. Try 'baton list'.")
    if len(hits) > 1:
        names = ", ".join(f"{a}:{f.stem[-8:]}" for a, f in hits[:5])
        raise BatonError(f"'{ref}' matches several sessions ({names}). Use a longer prefix.")
    agent, f = hits[0]
    return _loaded(PARSERS[agent](f))


def _loaded(s: Session) -> Session:
    if not s.users:
        seen = ", ".join(f"{k} x{v}" for k, v in s.skipped.most_common(6)) or "none"
        raise BatonError(
            f"Read {s.records} records from {s.path.name} but found no user messages. "
            f"The session format may have changed. Other record types seen: {seen}."
        )
    return s


# --------------------------------------------------------------------------- #
# Git (read-only, hardened against hostile repo config)
# --------------------------------------------------------------------------- #

def git(root: str, *args: str):
    cmd = [
        "git", "--no-optional-locks", "-C", root,
        "-c", "core.fsmonitor=false", "-c", "core.pager=cat", *args,
    ]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def git_root(cwd: str):
    out = git(cwd, "rev-parse", "--show-toplevel")
    return _real(out.strip()) if out and out.strip() else None


def ensure_excluded(root: str) -> None:
    """Keep .baton/ out of git without touching tracked files."""
    out = git(root, "rev-parse", "--git-path", "info/exclude")
    if not out:
        return
    p = Path(out.strip())
    if not p.is_absolute():
        p = Path(root) / p
    try:
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        if f"{OUT_DIR}/" not in existing.split():
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(("" if existing.endswith("\n") or not existing else "\n") + f"{OUT_DIR}/\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def clip(text: str, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text if len(text) <= limit else text[:limit].rstrip() + " [truncated]"


def tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "[...] " + text[-limit:].lstrip()


def oneline(text: str, limit: int) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    first = lines[0].strip() if lines else ""
    if len(lines) > 1:
        first += " [...]"
    return first if len(first) <= limit else first[:limit].rstrip() + " [...]"


def quote(text: str) -> str:
    return "\n".join("> " + l if l else ">" for l in text.splitlines())


def rel(path: str, root: str) -> str:
    if os.path.isabs(path) and path.startswith(root.rstrip(os.sep) + os.sep):
        return path[len(root.rstrip(os.sep)) + 1:]
    return path


def render(s: Session, root: str, note: str, go: bool):
    r = Redactor()
    c = r.clean
    when = datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M") if s.mtime else (s.ended or "unknown")
    L = [
        f"# Handoff: {Path(root).name}",
        "",
        f"Extracted automatically from a {LABELS[s.agent]} session (`{s.session_id[:8]}`, last activity {when}). "
        "It is a summary, not ground truth: verify it against the repo before acting on it.",
        "",
        "## Original request",
        "",
        quote(c(clip(s.users[0], MAX_REQUEST))),
        "",
    ]

    if s.compact:
        L += ["## Earlier context (the previous agent's own summary)", "", quote(c(clip(s.compact, MAX_COMPACT))), ""]

    recent = s.users[1:][-RECENT_USER_COUNT:]
    if recent:
        L += ["## Most recent requests (oldest first)", ""]
        for i, m in enumerate(recent, 1):
            L += [f"{i}. " + c(clip(m, MAX_RECENT_USER)).replace("\n", "\n   "), ""]

    if s.assistants:
        L += ["## What the previous agent said last", "", quote(c(clip(s.assistants[-1], MAX_LAST_ASSISTANT))), ""]

    if s.todos:
        marks = {"completed": "[x]", "in_progress": "[~]"}
        L += ["## Task list (last known)", ""]
        for status, text in s.todos[:MAX_TODOS]:
            L.append(f"- {marks.get(status, '[ ]')} {c(oneline(text, 160))}")
        L.append("")

    if s.files:
        items = list(reversed(list(s.files.items())))
        L += ["## Files the previous agent edited (most recent first)", ""]
        for p, n in items[:MAX_FILES]:
            L.append(f"- `{c(rel(p, root))}`" + (f" ({n}x)" if n > 1 else ""))
        if len(items) > MAX_FILES:
            L.append(f"- and {len(items) - MAX_FILES} more")
        L.append("")

    if s.commands:
        cmds = []
        for cmd in s.commands:
            if not cmds or cmds[-1] != cmd:
                cmds.append(cmd)
        L += ["## Last commands run", ""]
        for cmd in cmds[-MAX_COMMANDS:]:
            L.append(f"- `{c(oneline(cmd, MAX_COMMAND_CHARS)).replace('`', chr(39))}`")
        L.append("")

    if s.errors:
        L += ["## Recent errors (possible dead ends)", ""]
        for e in s.errors[-MAX_ERRORS:]:
            flat = " / ".join(l.strip() for l in tail(e, MAX_ERROR_CHARS).splitlines() if l.strip())
            L.append(f"- `{c(flat).replace('`', chr(39))}`")
        L.append("")

    branch = git(root, "branch", "--show-current")
    if branch is not None:
        L += ["## Repo state right now", ""]
        b = branch.strip() or (git(root, "rev-parse", "--short", "HEAD") or "").strip() or "unknown"
        L += [f"Branch: `{b}`", ""]
        sections = (
            ("Status", git(root, "status", "--short")),
            ("Diff vs HEAD", git(root, "diff", "HEAD", "--stat", "--no-ext-diff") or git(root, "diff", "--stat", "--no-ext-diff")),
            ("Recent commits", git(root, "log", "--oneline", "-5")),
        )
        for title, out in sections:
            if out and out.strip():
                lines = out.strip().splitlines()
                extra = len(lines) - MAX_GIT_LINES
                body = "\n".join(lines[:MAX_GIT_LINES]) + (f"\n... and {extra} more lines" if extra > 0 else "")
                L += [f"{title}:", "", "```", c(body), "```", ""]
        L = L if L[-1] == "" else L + [""]

    if note:
        L += ["## Note from the user", "", quote(c(clip(note, 1200))), ""]

    L += [
        "## How to continue",
        "",
        "1. Read this file, then check the repo state yourself (`git status`, `git diff`).",
        "2. " + (GO if go else CONFIRM),
        "",
    ]
    return "\n".join(L), r.count


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _ago(t: float) -> str:
    d = max(0, time.time() - t)
    if d < 90:
        return "just now"
    if d < 90 * 60:
        return f"{int(d / 60)} min ago"
    if d < 36 * 3600:
        return f"{int(d / 3600)} h ago"
    return f"{int(d / 86400)} d ago"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="baton",
        description="Hand a coding-agent session (Claude Code or Codex) to another agent.",
        epilog="examples:\n  baton\n  baton to codex\n  baton to claude -m 'next: fix the failing auth test'\n  baton list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("words", nargs="*", metavar="to claude|codex | list",
                   help="'to claude' or 'to codex' starts that agent on the handoff; 'list' shows sessions")
    p.add_argument("-m", "--note", default="", help="add a note for the next agent (for example, the next step)")
    p.add_argument("--from", dest="source", choices=AGENTS, help="only read sessions from this agent")
    p.add_argument("--session", metavar="ID|PATH", help="use a specific session (id prefix or file path)")
    p.add_argument("--out", metavar="PATH", help=f"write here instead of {OUT_DIR}/{OUT_FILE}")
    p.add_argument("--stdout", action="store_true", help="print the handoff instead of writing a file")
    p.add_argument("--go", action="store_true", help="tell the next agent to continue without confirming first")
    p.add_argument("--no-launch", action="store_true", help="with 'to X': write the file and print the command only")
    p.add_argument("-V", "--version", action="version", version=f"baton {__version__}")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    words = args.words
    target = None
    if not words:
        mode = "make"
    elif words == ["list"]:
        mode = "list"
    elif len(words) == 2 and words[0] == "to" and words[1] in AGENTS:
        mode, target = "to", words[1]
    else:
        parser.error("expected nothing, 'list', 'to claude' or 'to codex'")

    try:
        return _run(args, mode, target)
    except BatonError as e:
        print(f"baton: {e}", file=sys.stderr)
        return 1


def _run(args, mode: str, target) -> int:
    cwd = _real(os.getcwd())
    root = git_root(cwd) or cwd
    cwds = list(dict.fromkeys([cwd, root, os.getcwd()]))

    if mode == "list":
        rows = []
        for a in AGENTS:
            rows += [(_mtime(f), a, f) for f in CANDIDATES[a](cwds)]
        rows.sort(key=lambda x: x[0], reverse=True)
        if not rows:
            print(f"No Claude Code or Codex sessions found for {cwd}")
            return 1
        for mt, a, f in rows[:10]:
            s = PARSERS[a](f)
            first = oneline(Redactor().clean(s.users[0]), 60) if s.users else "(no messages)"
            print(f"{a:<7} {s.session_id[:8]}  {_ago(mt):<10} {first}")
        return 0

    if target and args.source == target:
        raise BatonError("--from and the target agent are the same.")
    sources = (args.source,) if args.source else tuple(a for a in AGENTS if a != target)

    s = load_explicit(args.session) if args.session else find_session(sources, cwds)
    if s is None:
        where = " or ".join(LABELS[a] for a in sources)
        raise BatonError(
            f"No {where} session found for {cwd}. Run baton from the project folder the agent worked in, "
            "or point at one with --session (see 'baton list')."
        )
    if target and s.agent == target:
        raise BatonError(f"That session is from {LABELS[target]} itself. Pick another with --session or --from.")

    text, redacted = render(s, root, args.note, args.go)
    say = (lambda m: print(m, file=sys.stderr)) if args.stdout else print

    if s.bad_lines and s.bad_lines > max(3, s.records // 10):
        say(f"baton: warning: {s.bad_lines} unreadable lines in the session; the handoff may be incomplete.")

    say(f"read   {LABELS[s.agent]} session {s.session_id[:8]} "
        f"({s.message_count} messages, last active {_ago(s.mtime)})")

    if args.stdout:
        sys.stdout.write(text)
        return 0

    out = Path(args.out).expanduser() if args.out else Path(root) / OUT_DIR / OUT_FILE
    out = out if out.is_absolute() else Path(os.getcwd()) / out
    _write_private(out, text)
    if not args.out:
        ensure_excluded(root)
    size = f"{len(text.encode('utf-8')) / 1024:.1f} KB"
    extra = f", {redacted} possible secret(s) redacted" if redacted else ""
    say(f"wrote  {out} ({size}{extra})")
    if redacted == 0:
        say("note   redaction is best effort; skim the file before sharing it anywhere")

    shown = rel(str(out), root)
    prompt = f"Read {shown} and continue the work described there. {GO if args.go else CONFIRM}"

    if not target:
        others = [a for a in AGENTS if a != s.agent]
        say(f"next   baton to {others[0]}")
        return 0

    exe = shutil.which(target)
    if args.no_launch or not exe:
        if not exe:
            say(f"baton: '{target}' is not on your PATH. Start it yourself and paste this prompt:")
        else:
            say("run    " + f'{target} "{prompt}"')
        if not exe:
            say(f"\n{prompt}")
        return 0 if exe else 1

    say(f"launch {target}")
    sys.stdout.flush()
    if os.name == "nt":
        return subprocess.call([exe, prompt])
    os.execv(exe, [exe, prompt])
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
