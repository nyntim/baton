import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import baton  # noqa: E402

SCRIPT = str(Path(__file__).parent / "baton.py")
FAKE_KEY = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def jl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def claude_rows(cwd):
    return [
        {"type": "summary", "summary": "Rate limiting", "leafUuid": "x"},
        {"type": "user", "cwd": cwd, "gitBranch": "feat/rate", "timestamp": "2026-10-20T01:00:00.000Z",
         "message": {"role": "user", "content": f"Add rate limiting to the API. Test with key {FAKE_KEY}"}},
        {"type": "assistant", "timestamp": "2026-10-20T01:01:00.000Z", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "SECRET THOUGHTS"},
            {"type": "text", "text": "I'll start with the middleware."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{cwd}/src/app.py"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": f"{cwd}/src/limiter.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": True, "content": "Traceback (most recent call last)\nValueError: boom"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -x"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{cwd}/src/app.py"}},
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                {"content": "Write limiter", "status": "completed"},
                {"content": "Add tests", "status": "in_progress"},
                {"content": "Docs", "status": "pending"}]}},
        ]}},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "SIDECHAIN MSG"}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "META MSG"}},
        {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {"type": "user", "message": {"role": "user", "content": "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>"}},
        {"type": "user", "timestamp": "2026-10-20T01:30:00.000Z",
         "message": {"role": "user", "content": "Now also add tests for the limiter"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Adding tests next, starting with the burst case."}]}},
    ]


def codex_rows(cwd):
    return [
        {"timestamp": "2026-10-20T02:00:00Z", "type": "session_meta",
         "payload": {"id": "019c0000-1111-2222-3333-444455556666", "cwd": cwd, "git": {"branch": "main"}}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n<cwd>/x</cwd>\n</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Refactor the auth module to use sessions"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Starting with the session store."}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
         "arguments": json.dumps({"command": ["bash", "-lc", "pytest -q"]})}},
        {"type": "response_item", "payload": {"type": "function_call_output",
         "output": json.dumps({"output": "FAILED tests/test_auth.py::test_login", "metadata": {"exit_code": 1}})}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch",
         "arguments": json.dumps({"input": "*** Begin Patch\n*** Update File: src/auth.py\n@@\n*** Add File: src/store.py\n*** End Patch"})}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan",
         "arguments": json.dumps({"plan": [{"step": "Session store", "status": "completed"},
                                           {"step": "Migrate login", "status": "in_progress"}]})}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "duplicate, must be ignored"}},
    ]


CODEX_USER_ID = "01a15878-4937-71b2-b9da-06fa265e94f3"
CODEX_CHILD_ID = "01a15878-6b10-7a21-8c3e-1f2a3b4c5d6e"


def _cell(*outputs, header="Script completed\nWall time 0.3 seconds\nOutput:\n"):
    return [{"type": "input_text", "text": header}] + [{"type": "input_text", "text": json.dumps(o)} for o in outputs]


def codex_code_mode_rows(cwd, sid=CODEX_USER_ID):
    # Shapes copied from real 2026-10 rollouts: tools run from a JS "exec" cell, outputs are block lists.
    patch = f"*** Begin Patch\n*** Update File: {cwd}/src/auth.py\n@@\n*** Add File: src/store.py\n+x\n*** End Patch\n"
    cmd = 'rg -n "login" src && pytest -q'
    return [
        {"timestamp": "2026-10-20T02:00:00Z", "type": "session_meta", "payload": {
            "id": sid, "session_id": sid, "cwd": cwd, "originator": "codex-tui", "source": "vscode",
            "thread_source": "user", "git": {"branch": "dev/x"}}},
        {"type": "turn_context", "payload": {"cwd": cwd, "turn_id": "t1", "model": "m"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "<permissions instructions>\nsandbox"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": f"# AGENTS.md instructions for {cwd}\n\n<INSTRUCTIONS>x</INSTRUCTIONS>"},
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<skill>\n<name>ship</name>\n<path>/x/SKILL.md</path>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Refactor the auth module to use sessions"}]}},
        {"type": "event_msg", "payload": {"type": "item_completed"}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [], "encrypted_content": "zz"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input":
            f"const r=await tools.exec_command({{cmd:{json.dumps(cmd)},max_output_tokens:8000}});\n"
            'text(await tools.web__run({search_query:[{q:"x"}]}));\n'
            'text(await tools.write_stdin({session_id:1,chars:"\\u0003"}));\n'}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": _cell(
            {"chunk_id": "a", "exit_code": 1, "output": "FAILED tests/test_auth.py::test_login"})}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c2",
                                              "input": f"text(await tools.apply_patch({json.dumps(patch)}));\n"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": _cell(
            {"chunk_id": "b", "exit_code": 0, "output": "Success."})}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c3",
                                              "input": "text(await tools.exec_command({cmd:'git status --short'}));"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c3",
                                              "output": _cell(header="Script error: ReferenceError: r is not defined")}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [
            {"type": "output_text", "text": "Session store is in; login test still fails."}]}},
    ]


def codex_child_rows(cwd):
    # A forked subagent thread: its own session_meta, then the parent's, then the copied parent history.
    own = {"id": CODEX_CHILD_ID, "cwd": cwd, "thread_source": "subagent", "parent_thread_id": CODEX_USER_ID,
           "source": {"subagent": {"thread_spawn": {"parent_thread_id": CODEX_USER_ID, "agent_role": "builder"}}}}
    rows = codex_code_mode_rows(cwd)
    return [{"type": "session_meta", "payload": own}] + rows + [
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "CHILD TASK: build the store"}]}}]


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_claude(self):
        f = self.tmp / "abc123.jsonl"
        jl(f, claude_rows("/work/proj"))
        s = baton.parse_claude(f)
        self.assertEqual(len(s.users), 2)
        self.assertTrue(s.users[0].startswith("Add rate limiting"))
        self.assertNotIn("SIDECHAIN MSG", s.users)
        self.assertNotIn("META MSG", s.users)
        self.assertEqual(s.assistants[-1], "Adding tests next, starting with the burst case.")
        self.assertNotIn("SECRET THOUGHTS", " ".join(s.assistants))
        self.assertEqual(list(s.files), ["/work/proj/src/limiter.py", "/work/proj/src/app.py"])
        self.assertEqual(s.files["/work/proj/src/app.py"], 2)
        self.assertEqual(s.commands, ["pytest -x"])
        self.assertEqual(len(s.errors), 1)
        self.assertEqual(s.todos[1], ("in_progress", "Add tests"))
        self.assertEqual(s.cwd, "/work/proj")
        self.assertEqual(s.branch, "feat/rate")

    def test_claude_compact_summary(self):
        f = self.tmp / "c.jsonl"
        jl(f, claude_rows("/w") + [{"type": "user", "isCompactSummary": True,
                                    "message": {"role": "user", "content": "This session is being continued. Summary: X"}}])
        s = baton.parse_claude(f)
        self.assertIn("Summary: X", s.compact)
        self.assertEqual(len(s.users), 2)

    def test_codex(self):
        f = self.tmp / "rollout-2026-10-20T02-00-00-019c0000-1111-2222-3333-444455556666.jsonl"
        jl(f, codex_rows("/work/proj"))
        s = baton.parse_codex(f)
        self.assertEqual(s.users, ["Refactor the auth module to use sessions"])
        self.assertEqual(s.assistants, ["Starting with the session store."])
        self.assertEqual(s.commands, ["pytest -q"])
        self.assertEqual(list(s.files), ["src/auth.py", "src/store.py"])
        self.assertEqual(len(s.errors), 1)
        self.assertIn("FAILED", s.errors[0])
        self.assertEqual(s.todos[1], ("in_progress", "Migrate login"))
        self.assertEqual(s.cwd, "/work/proj")
        self.assertEqual(s.session_id, "019c0000-1111-2222-3333-444455556666")

    def test_codex_code_mode(self):
        f = self.tmp / f"rollout-2026-10-20T02-00-00-{CODEX_USER_ID}.jsonl"
        jl(f, codex_code_mode_rows("/work/proj"))
        s = baton.parse_codex(f)
        self.assertEqual(s.users, ["Refactor the auth module to use sessions"])
        self.assertEqual(s.assistants, ["Session store is in; login test still fails."])
        self.assertEqual(s.commands, ['rg -n "login" src && pytest -q', "git status --short"])
        self.assertEqual(list(s.files), ["/work/proj/src/auth.py", "src/store.py"])
        self.assertEqual(len(s.errors), 2)
        self.assertEqual(s.errors[0], "FAILED tests/test_auth.py::test_login")
        self.assertIn("ReferenceError", s.errors[1])
        self.assertEqual((s.cwd, s.branch, s.session_id), ("/work/proj", "dev/x", CODEX_USER_ID))

    def test_codex_fork_keeps_its_own_id(self):
        f = self.tmp / f"rollout-2026-10-20T02-05-00-{CODEX_CHILD_ID}.jsonl"
        jl(f, codex_child_rows("/work/proj"))
        self.assertEqual(baton.parse_codex(f).session_id, CODEX_CHILD_ID)
        self.assertTrue(baton._codex_is_child(baton._head(f)))

    def test_js_string_literals(self):
        self.assertEqual(baton._js_str(r'"a \"b\"\nc"'), 'a "b"\nc')
        self.assertEqual(baton._js_str(r"'it\'s'"), "it's")
        self.assertEqual(baton._js_str(r"`x\ty`"), "x\ty")

    def test_unknown_format_gives_clear_error(self):
        f = self.tmp / "weird.jsonl"
        jl(f, [{"type": "brand_new_thing", "x": 1}] * 3)
        with self.assertRaises(baton.BatonError) as cm:
            baton.load_explicit(str(f))
        self.assertIn("format may have changed", str(cm.exception))
        self.assertIn("brand_new_thing", str(cm.exception))

    def test_bad_lines_do_not_crash(self):
        f = self.tmp / "bad.jsonl"
        f.write_text('not json\n{"type":"user","message":{"content":"hello there"}}\n[1,2]\n')
        s = baton.parse_claude(f)
        self.assertEqual(s.users, ["hello there"])
        self.assertEqual(s.bad_lines, 2)


class RedactionTests(unittest.TestCase):
    def test_patterns(self):
        samples = [
            FAKE_KEY,
            "sk-proj-" + "a" * 30,
            "ghp_" + "b" * 36,
            "AKIAABCDEFGHIJKLMNOP",
            "xoxb-123456789012-abcdefghij",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "postgres://admin:hunter2pass@db.example.com:5432/app",
            "DATABASE_PASSWORD=supersecret99",
            'API_TOKEN: "abcd1234efgh"',
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----",
        ]
        for text in samples:
            r = baton.Redactor()
            out = r.clean(f"before {text} after")
            self.assertIn("[REDACTED]", out, text)
            self.assertGreaterEqual(r.count, 1, text)
        r = baton.Redactor()
        self.assertEqual(r.clean("plain text, max_tokens: 4096, nothing here"), "plain text, max_tokens: 4096, nothing here")
        self.assertEqual(r.count, 0)

    def test_no_double_count(self):
        r = baton.Redactor()
        r.clean("API_KEY=abcdefghijk1234")
        self.assertEqual(r.count, 1)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = Path(os.path.realpath(self.tmp / "proj"))
        self.repo.mkdir()
        run = lambda *a: subprocess.run(a, cwd=self.repo, check=True, capture_output=True)
        run("git", "init", "-q")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        (self.repo / "a.txt").write_text("one\n")
        run("git", "add", ".")
        run("git", "commit", "-qm", "init")
        (self.repo / "a.txt").write_text("one\ntwo\n")
        self.claude = self.tmp / "claude"
        self.codex = self.tmp / "codex"
        enc = re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
        jl(self.claude / "projects" / enc / "abcd1234-0000-4000-8000-000000000001.jsonl", claude_rows(str(self.repo)))
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.claude), CODEX_HOME=str(self.codex))
        self._old = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        os.environ.update(CLAUDE_CONFIG_DIR=str(self.claude), CODEX_HOME=str(self.codex))
        self._cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._cwd)
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = baton.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_make_writes_private_redacted_file(self):
        code, out, _ = self.call()
        self.assertEqual(code, 0, out)
        f = self.repo / ".baton" / "HANDOFF.md"
        text = f.read_text()
        self.assertIn("Add rate limiting", text)
        self.assertIn("Now also add tests", text)
        self.assertIn("Adding tests next", text)
        self.assertIn("- [~] Add tests", text)
        self.assertIn("`src/app.py` (2x)", text)
        self.assertIn("`pytest -x`", text)
        self.assertIn("ValueError: boom", text)
        self.assertIn("Branch:", text)
        self.assertIn("a.txt", text)  # live git status / diff
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("SECRET THOUGHTS", text)
        self.assertIn("redacted", out)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        exclude = (self.repo / ".git" / "info" / "exclude").read_text()
        self.assertIn(".baton/", exclude)
        self.assertIn("baton to codex", out)
        status = subprocess.run(["git", "status", "--short"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertNotIn(".baton", status)

    def test_note_and_go(self):
        self.call("-m", "next: fix limiter burst bug", "--go")
        text = (self.repo / ".baton" / "HANDOFF.md").read_text()
        self.assertIn("next: fix limiter burst bug", text)
        self.assertIn(baton.GO, text)
        self.assertNotIn("wait for my go-ahead", text)

    def test_stdout(self):
        code, out, err = self.call("--stdout")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# Handoff:"))
        self.assertFalse((self.repo / ".baton").exists())

    def test_works_from_subdirectory(self):
        sub = self.repo / "src"
        sub.mkdir()
        os.chdir(sub)
        code, _, _ = self.call()
        self.assertEqual(code, 0)
        self.assertTrue((self.repo / ".baton" / "HANDOFF.md").exists())

    def test_codex_session_preferred_when_newer(self):
        f = self.codex / "sessions" / "2026" / "10" / "20" / "rollout-2026-10-20T02-00-00-019c0000-1111-2222-3333-444455556666.jsonl"
        jl(f, codex_rows(str(self.repo)))
        future = os.path.getmtime(f) + 100
        os.utime(f, (future, future))
        code, out, _ = self.call("--stdout")
        self.assertIn("Refactor the auth module", out)
        code, out, _ = self.call("--stdout", "--from", "claude")
        self.assertIn("Add rate limiting", out)

    def test_codex_child_threads_are_ignored(self):
        day = self.codex / "sessions" / "2026" / "10" / "20"
        user = day / f"rollout-2026-10-20T02-00-00-{CODEX_USER_ID}.jsonl"
        child = day / f"rollout-2026-10-20T02-05-00-{CODEX_CHILD_ID}.jsonl"
        jl(user, codex_code_mode_rows(str(self.repo)))
        jl(child, codex_child_rows(str(self.repo)))
        now = os.path.getmtime(user)
        os.utime(user, (now + 100, now + 100))
        os.utime(child, (now + 200, now + 200))  # the child is newest, but must never be picked
        code, out, _ = self.call("list")
        self.assertEqual(out.count("codex"), 1, out)
        code, out, _ = self.call("--stdout")
        self.assertIn("Refactor the auth module", out)
        self.assertNotIn("CHILD TASK", out)
        code, out, err = self.call("--stdout", "--session", CODEX_USER_ID[:8])  # the prefix both ids share
        self.assertEqual(code, 0, err)
        self.assertNotIn("CHILD TASK", out)
        code, out, err = self.call("--stdout", "--session", CODEX_CHILD_ID)  # still readable when asked for
        self.assertEqual(code, 0, err)
        self.assertIn("CHILD TASK", out)

    def test_list(self):
        code, out, _ = self.call("list")
        self.assertEqual(code, 0)
        self.assertIn("claude", out)
        self.assertIn("abcd1234", out)
        self.assertNotIn("sk-ant", out)
        self.assertIn("[REDACTED]", out)

    def test_session_by_prefix(self):
        code, out, _ = self.call("--stdout", "--session", "abcd1234")
        self.assertEqual(code, 0)

    def test_no_session(self):
        other = self.tmp / "empty"
        other.mkdir()
        os.chdir(other)
        code, _, err = self.call()
        self.assertEqual(code, 1)
        self.assertIn("No", err)

    def test_target_same_as_source(self):
        code, _, err = self.call("to", "claude", "--from", "claude")
        self.assertEqual(code, 1)

    def test_to_claude_rejects_claude_session(self):
        code, _, err = self.call("to", "claude", "--session", "abcd1234")
        self.assertEqual(code, 1)
        self.assertIn("itself", err)

    def test_missing_binary_is_graceful(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = str(self.tmp)
        try:
            code, out, err = self.call("to", "codex")
        finally:
            os.environ["PATH"] = old
        self.assertEqual(code, 1)
        self.assertTrue((self.repo / ".baton" / "HANDOFF.md").exists())
        self.assertIn("not on your PATH", out + err)

    def test_launch_passes_prompt(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        argfile = self.tmp / "args.txt"
        fake = bindir / "codex"
        fake.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{argfile}"\n')
        fake.chmod(0o755)
        env = dict(self.env, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        r = subprocess.run([sys.executable, SCRIPT, "to", "codex"], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        prompt = argfile.read_text()
        self.assertIn(".baton/HANDOFF.md", prompt)
        self.assertIn("wait for my go-ahead", prompt)

    def test_no_launch_prints_command(self):
        bindir = self.tmp / "bin2"
        bindir.mkdir()
        fake = bindir / "codex"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{old}"
        try:
            code, out, _ = self.call("to", "codex", "--no-launch")
        finally:
            os.environ["PATH"] = old
        self.assertEqual(code, 0)
        self.assertIn('codex "Read .baton/HANDOFF.md', out)

    def test_bad_usage(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                baton.main(["to", "gemini"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
