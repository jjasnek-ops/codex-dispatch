#!/usr/bin/env python3
"""Regression tests for startup update handling and worker health gates."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import shlex
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_run  # noqa: E402
import watchdog  # noqa: E402

RUNNER = ROOT / "codex_run.py"
FAKE_CODEX = Path(__file__).with_name("fake_codex_worker.py")
NOTIFY_HOOK = ROOT / "hooks" / "notify-agi.sh"


class DispatchHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A fresh clone has no ignored tmp directory. Keep fixtures local even
        # when the caller did not set TMPDIR, and restore tempfile afterwards.
        directory = ROOT / "tmp"
        directory.mkdir(parents=True, exist_ok=True)
        temp_patch = patch.object(tempfile, "tempdir", str(directory))
        temp_patch.start()
        cls.addClassCleanup(temp_patch.stop)

    def isolated_runtime(self, directory):
        runtime = directory / "runtime"
        runtime.mkdir()
        (runtime / "hooks").mkdir()
        for name in ("codex_run.py", "watchdog.py", "task_state.py"):
            shutil.copy2(ROOT / name, runtime / name)
        shutil.copy2(NOTIFY_HOOK, runtime / "hooks/notify-agi.sh")
        return runtime

    def isolated_runner(self, runtime):
        # The real runner configures trust/notify before launch. Point those
        # module globals at the fixture, never at the user's ~/.codex files.
        code = ("import sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv.pop(1)); import codex_run as r; "
                "r.CODEX_AUTH_FILE=Path(sys.path[0])/'auth.json'; "
                "r.CODEX_CONFIG_FILE=Path(sys.path[0])/'config.toml'; "
                "r.CODEX_AUTH_MODE=''; raise SystemExit(r.main())")
        return [sys.executable, "-c", code, str(runtime)]

    def capacity_args(self):
        return argparse.Namespace(socket="unused.sock", capacity_retries=3, capacity_delay_s=30,
            capacity_max_delay_s=300, capacity_response_timeout_s=10, capacity_input=watchdog.DEFAULT_CAPACITY_INPUT)

    def test_empty_tmux_context_is_never_a_valid_identity(self):
        with patch.object(codex_run.subprocess, "check_output", return_value=":\n"):
            with self.assertRaisesRegex(ValueError, "invalid_tmux_instance"):
                codex_run.tmux_session_instance("unused", "fixture")

    def test_prompt_echo_and_existing_work_do_not_confirm_submission(self):
        before = "• Working on previous request\n› Ask Codex to do anything\n"
        self.assertFalse(codex_run.prompt_response_observed(before, before + "› new prompt\n"))
        self.assertTrue(codex_run.prompt_response_observed(before, before + "• Ran python3\n"))

    def test_real_parallel_prompt_pastes_use_task_local_named_buffers(self):
        with tempfile.TemporaryDirectory(prefix="paste-") as directory:
            root = Path(directory)
            socket = str(root / "test.sock")
            raw = subprocess.check_call
            a_loaded, b_loaded, a_pasted = (threading.Event() for _ in range(3))
            errors = []
            def call(command, *args, **kwargs):
                role = threading.current_thread().name
                if "load-buffer" in command:
                    if role == "B":
                        self.assertTrue(a_loaded.wait(5))
                    code = raw(command, *args, **kwargs)
                    if role == "A":
                        a_loaded.set()
                        self.assertTrue(b_loaded.wait(5))
                    else:
                        b_loaded.set()
                    return code
                if "paste-buffer" in command:
                    if role == "B":
                        self.assertTrue(a_pasted.wait(5))
                    code = raw(command, *args, **kwargs)
                    if role == "A":
                        a_pasted.set()
                    return code
                return raw(command, *args, **kwargs)
            def send(role):
                try:
                    codex_run.tmux_paste_prompt(socket, role + ":0.0", "FIXTURE_TASK_" + role)
                except BaseException as error:
                    errors.append(repr(error))
            try:
                for role in ("A", "B"):
                    raw(["tmux", "-f", "/dev/null", "-S", socket, "new-session", "-d", "-s", role,
                         "cat > " + shlex.quote(str(root / (role + ".input")))])
                raw(["tmux", "-S", socket, "set-buffer", "-b", "unrelated", "preserve me"])
                with patch.object(codex_run.subprocess, "check_call", side_effect=call):
                    threads = [threading.Thread(name=role, target=send, args=(role,)) for role in ("A", "B")]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(10)
                        self.assertFalse(thread.is_alive())
                self.assertEqual([], errors)
                for role in ("A", "B"):
                    self.assertEqual("FIXTURE_TASK_" + role, (root / (role + ".input")).read_text().strip())
                buffers = subprocess.check_output(["tmux", "-S", socket, "list-buffers", "-F", "#{buffer_name}"], text=True)
                self.assertEqual(["unrelated"], buffers.splitlines())
            finally:
                subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True)

    def test_failed_paste_cleans_only_its_named_buffer_and_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix="paste-fail-") as directory:
            root = Path(directory)
            calls = []
            def call(command):
                calls.append(command)
                if "paste-buffer" in command:
                    raise subprocess.CalledProcessError(1, command)
                return 0
            with patch.object(codex_run, "SCRIPT_DIR", root), \
                    patch.object(codex_run.subprocess, "check_call", side_effect=call), \
                    patch.object(codex_run.subprocess, "run") as cleanup:
                with self.assertRaises(subprocess.CalledProcessError):
                    codex_run.tmux_paste_prompt("unused", "fixture:0.0", "fixture")
            loaded = calls[0][calls[0].index("-b") + 1]
            self.assertEqual(loaded, cleanup.call_args.args[0][-1])
            self.assertIn("delete-buffer", cleanup.call_args.args[0])
            self.assertEqual([], list((root / "tmp").iterdir()))

    def test_interactive_resume_is_passed_to_codex_without_picker(self):
        args = argparse.Namespace(codex_bin="fixture-codex", cwd=str(ROOT), resume="existing-thread",
            model="", yolo=True, sandbox=None, allow_update_check=False)
        command = codex_run.build_tmux_cmd(args)
        index = command.index("resume")
        self.assertEqual("existing-thread", command[index + 1])
        self.assertNotIn("--last", command)
        self.assertIn("--no-daemon", command)
        self.assertNotIn("-m", command)
        args.resume = None
        self.assertNotIn("resume", codex_run.build_tmux_cmd(args))

    def test_parallel_runtime_configuration_never_writes_shared_config(self):
        with tempfile.TemporaryDirectory(prefix="config-") as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = b'model = "fixture"\n# preserve comments and custom settings\n'
            config.write_bytes(original)
            projects = [root / ("project-" + str(i)) for i in range(8)]
            for project in projects:
                project.mkdir()
            def build(project):
                args = argparse.Namespace(codex_bin="fixture-codex", cwd=str(project), resume=None,
                    model="", yolo=True, sandbox=None, allow_update_check=False)
                return codex_run.build_tmux_cmd(args)
            with patch.object(codex_run, "CODEX_CONFIG_FILE", config), ThreadPoolExecutor(max_workers=8) as pool:
                commands = list(pool.map(build, projects))
            self.assertEqual(original, config.read_bytes())
            for project, command in zip(projects, commands):
                override = next(value for value in command if value.startswith("projects="))
                self.assertEqual(tomllib.loads(override)["projects"][str(project)]["trust_level"], "trusted")
                self.assertIn("notify=" + json.dumps([codex_run.NOTIFY_SCRIPT]), command)

    def hook_states(self, output, *, interactive=True, exit_code=0):
        with tempfile.TemporaryDirectory(prefix="hook-state-") as directory:
            root = Path(directory)
            runtime = self.isolated_runtime(root)
            session = root / "session"
            session.mkdir()
            meta = session / "task-meta.json"
            stale = {"task_id": "state-test", "status": "running", "feishu_target": "",
                     "lifecycle_stage": "business_started", "completed_at": "old", "exit_code": 0}
            meta.write_text(json.dumps(stale))
            task = runtime / "data/tasks/state-test.json"
            task.parent.mkdir(parents=True)
            task.write_text(json.dumps(stale))
            env = {**os.environ, "CODING_AGENT_TASK_ID": "state-test",
                   "CODING_AGENT_SESSION_DIR": str(session), "CODEX_EXIT_CODE": str(exit_code)}
            command = ["bash", str(runtime / "hooks/notify-agi.sh")]
            if interactive:
                command.append(json.dumps({"type": "agent-turn-complete", "thread-id": "fixture",
                    "turn-id": "new-turn", "last-assistant-message": output}))
            else:
                last = root / "last.txt"
                last.write_text(output)
                env["CODEX_LAST_MSG_FILE"] = str(last)
            subprocess.run(command, env=env, check=True, capture_output=True)
            return [json.loads(p.read_text()) for p in (meta, task, runtime / "data/latest.json")]

    def test_hook_progress_formats_never_become_task_completion(self):
        outputs = ['{"title":"Part 2 complete"}', '{\n  "title": "checking Part 2"\n}',
            ' \n {"detail":"✅ complete", "title":"checking"}',
            '{"status":"running", "ok":true, "child":{"status":"success"}}',
            '{"status":"in_progress"}', '{"status":"retrying"}', '{}',
            '✅ Part 2 completed; git commit created', 'Working on the next step',
            '{"ok":true,"status":"success"}\n{"status":"running"}']
        for output in outputs:
            with self.subTest(output=output):
                for state in self.hook_states(output):
                    self.assertEqual(state["status"], "running")
                    self.assertEqual(state["lifecycle_stage"], "business_started")
                    self.assertNotIn("completed_at", state)
                    self.assertNotIn("exit_code", state)
                    if output.lstrip().startswith('{"title":') or output.startswith('{\n  "title":'):
                        self.assertNotIn("thread_id", state)
                        self.assertNotIn("last_turn_completed_at", state)
                    else:
                        self.assertTrue(state["last_turn_completed_at"])

    def test_hook_title_does_not_override_explicit_terminal_status(self):
        for status in ("success", "success_with_warnings"):
            with self.subTest(status=status):
                output = json.dumps({"title": "final", "ok": True, "status": status,
                                     "child": {"exit_code": 1}}, indent=2)
                for state in self.hook_states(output):
                    self.assertEqual(state["status"], "done")
                    self.assertEqual(state["lifecycle_stage"], "completed")
                    self.assertTrue(state["completed_at"])
                    self.assertEqual(state["exit_code"], 0)

    def test_hook_waiting_input_is_not_completed_in_any_mirror(self):
        for state in self.hook_states('{"status":"waiting_input","title":"need user"}'):
            self.assertEqual(state["status"], "waiting_input")
            self.assertNotIn("completed_at", state)
            self.assertNotIn("exit_code", state)

    def test_hook_nonzero_exit_overrides_success_and_headless_progress_needs_input(self):
        for state in self.hook_states('{"ok":true,"status":"success"}', exit_code=1):
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["exit_code"], 1)
        for state in self.hook_states('{"status":"running"}', interactive=False):
            self.assertEqual(state["status"], "waiting_input")
            self.assertNotIn("completed_at", state)
        for state in self.hook_states('Legacy headless result', interactive=False):
            self.assertEqual(state["status"], "done")

    def test_dispatch_emits_terminal_contract_and_hook_accepts_fixture_result(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-contract-") as directory:
            root = Path(directory)
            runtime = self.isolated_runtime(root)
            shutil.copy2(ROOT / "dispatch-codex.sh", runtime / "dispatch-codex.sh")
            # Replace only the external worker in this isolated copy. Execute
            # the current dispatch and Hook, never a real Codex/API/login.
            (runtime / "codex_run.py").write_text('''import json, os, sys
from pathlib import Path
prompt = Path(os.environ["CODING_AGENT_PROMPT_FILE"]).read_text()
assert "status=running" in prompt and "单个顶层 JSON" in prompt
assert "success_with_warnings" in prompt and "status=waiting_input" in prompt
output = Path(sys.argv[sys.argv.index("--output-file") + 1])
output.write_text(json.dumps({"ok": True, "status": "success", "summary": "fixture only"}))
''')
            # Both notification paths use timeout; this shim records and blocks
            # outbound calls without modifying the machine's OpenClaw binary.
            bin_dir = root / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "timeout"
            shim.write_text('#!/bin/sh\n[ "$1" = "--kill-after=5" ] && shift\n[ "$2" = "/usr/bin/openclaw" ] || exit 95\nprintf "blocked\\n" >> "$FIXTURE_NOTIFY_LOG"\n')
            shim.chmod(0o755)
            config = root / "dispatch-config.json"
            config.write_text('{}')
            notice_log = root / "blocked-notifications.log"
            result = subprocess.run(["bash", str(runtime / "dispatch-codex.sh"), "--no-tmux",
                "--target", "fixture-no-network", "-w", str(root), "-p", "fixture original request"],
                env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                     "DISPATCH_CONFIG": str(config), "CDP_PORT": "", "FIXTURE_NOTIFY_LOG": str(notice_log)},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            meta = json.loads(next((runtime / "data/sessions").glob("*/task-meta.json")).read_text())
            self.assertEqual(meta["status"], "done")
            self.assertIn("fixture original request", Path(meta["prompt_file"]).read_text())
            for path in (runtime / "data/tasks" / (meta["task_id"] + ".json"), runtime / "data/latest.json"):
                self.assertEqual(json.loads(path.read_text())["status"], "done")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                receipts = list((runtime / "data/outbox").glob("*.json"))
                if len(receipts) == 2 and all(json.loads(p.read_text())["state"] == "delivered" for p in receipts):
                    break
                time.sleep(0.05)
            self.assertEqual(len(notice_log.read_text().splitlines()), 2)

    def test_capacity_empty_error_disappearance_and_echo_are_not_acknowledgements(self):
        args = self.capacity_args()
        blocked = "■ Selected model is at capacity. Please try a different model."
        with patch.object(watchdog, "send_capacity_input") as send:
            initial, _, _ = watchdog.handle_capacity(args, {}, "test", blocked, 100)
            for pane in ("", "› " + args.capacity_input, "› Ready", "The UI says esc to interrupt"):
                with self.subTest(pane=pane):
                    update, done, _ = watchdog.handle_capacity(args, initial, "test", pane, 105)
                    self.assertEqual(update["health_status"], "capacity_waiting_response")
                    self.assertNotIn("capacity_retry_response_at", update)
                    self.assertFalse(done)
            self.assertEqual(send.call_count, 1)

    def test_capacity_old_working_after_screen_clear_is_not_a_new_turn(self):
        args = self.capacity_args()
        old = "• Working (1s • esc to interrupt)\nSelected model is at capacity.\nPlease try a different model."
        self.assertTrue(watchdog.model_capacity_blocked(old))
        with patch.object(watchdog, "send_capacity_input"):
            initial, _, _ = watchdog.handle_capacity(args, {}, "test", old, 100)
            result, _, _ = watchdog.handle_capacity(args, initial, "test", "• Working (40s • esc to interrupt)", 105)
            self.assertEqual(result["health_status"], "capacity_waiting_response")
            fresh = old + "\n› continuation\n• Working (1s • esc to interrupt)"
            result, _, _ = watchdog.handle_capacity(args, initial, "test", fresh, 106)
            self.assertEqual(result["health_status"], "active")

    def test_capacity_changed_pane_does_not_bypass_backoff(self):
        args = self.capacity_args()
        pane = "Selected model is at capacity. Please try a different model."
        with patch.object(watchdog, "send_capacity_input") as send:
            meta, _, _ = watchdog.handle_capacity(args, {}, "test", pane, 100)
            timed_out, _, _ = watchdog.handle_capacity(args, meta, "test", pane, 111)
            meta.update(timed_out)
            for t in (112, 120, 140):
                update, done, _ = watchdog.handle_capacity(args, meta, "test", pane + f"\n› echo {t}", t)
                self.assertEqual(update["health_status"], "capacity_waiting")
                self.assertFalse(done)
            self.assertEqual(send.call_count, 1)
            update, _, _ = watchdog.handle_capacity(args, meta, "test", pane + "\n› echo", 141)
            self.assertEqual(update["capacity_retry_count"], 2)
            self.assertEqual(send.call_count, 2)

    def test_capacity_late_response_is_accepted_without_duplicate_send(self):
        args = self.capacity_args()
        pane = "Selected model is at capacity. Please try a different model."
        with patch.object(watchdog, "send_capacity_input") as send:
            meta, _, _ = watchdog.handle_capacity(args, {}, "test", pane, 100)
            meta.update(watchdog.handle_capacity(args, meta, "test", pane, 111)[0])
            result, _, _ = watchdog.handle_capacity(args, meta, "test", "• Working (1s • esc to interrupt)", 112)
            self.assertEqual(result["capacity_retry_count"], 0)
            self.assertEqual(result["health_status"], "active")
            self.assertEqual(send.call_count, 1)

    def test_capacity_new_error_after_working_is_not_acknowledgement(self):
        args = self.capacity_args()
        pane = "Selected model is at capacity. Please try a different model."
        with patch.object(watchdog, "send_capacity_input"):
            meta, _, _ = watchdog.handle_capacity(args, {}, "test", pane, 100)
            result, _, _ = watchdog.handle_capacity(args, meta, "test", "• Working (1s • esc to interrupt)\n" + pane, 102)
            self.assertEqual(result["health_status"], "capacity_waiting_response")

    def test_capacity_send_failure_is_bounded_and_not_a_response(self):
        args = self.capacity_args()
        args.capacity_retries = 1
        pane = "Selected model is at capacity. Please try a different model."
        with patch.object(watchdog, "send_capacity_input", side_effect=OSError("fixture error")) as send:
            meta, _, _ = watchdog.handle_capacity(args, {}, "test", pane, 100)
            self.assertEqual(meta["health_status"], "capacity_retry_send_failed")
            self.assertNotIn("capacity_retry_response_at", meta)
            result, done, _ = watchdog.handle_capacity(args, meta, "test", pane, 131)
            self.assertTrue(done)
            self.assertEqual(result["status"], "waiting_input")
            self.assertEqual(send.call_count, 1)

    def test_capacity_input_settles_before_enter_and_never_clears_draft(self):
        events = []
        with patch.object(watchdog.subprocess, "check_call", side_effect=lambda cmd: events.append(cmd)), \
             patch.object(watchdog.time, "sleep", side_effect=lambda seconds: events.append(seconds)):
            watchdog.send_capacity_input("fixture.sock", "task:0.0", "continue safely")
        self.assertEqual(events[0][-3:], ["-l", "--", "continue safely"])
        self.assertEqual(events[1], 0.2)
        self.assertEqual(events[2][-1], "Enter")
        self.assertEqual(len(events), 3)

    def test_capacity_real_tmux_submit_is_accepted_by_isolated_worker(self):
        with tempfile.TemporaryDirectory(prefix="cap-", dir=ROOT / "tmp") as directory:
            socket = str(Path(directory) / "test.sock")
            args = self.capacity_args()
            args.socket = socket
            target = "capacity-fixture:0.0"
            subprocess.run(["tmux", "-S", socket, "new-session", "-d", "-s", "capacity-fixture",
                sys.executable, str(FAKE_CODEX)],
                env={**os.environ, "FAKE_CODEX_CAPACITY": "1", "FAKE_CODEX_UPDATE_ONCE": ""}, check=True)
            try:
                deadline = time.monotonic() + 5
                pane = ""
                while time.monotonic() < deadline:
                    pane = watchdog.tmux_capture(socket, target)
                    if watchdog.model_capacity_blocked(pane):
                        break
                    time.sleep(0.1)
                self.assertTrue(watchdog.model_capacity_blocked(pane))
                meta, _, _ = watchdog.handle_capacity(args, {}, target, pane, 100)
                self.assertEqual(meta["health_status"], "capacity_waiting_response")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    pane = watchdog.tmux_capture(socket, target)
                    if "fake prompt accepted" in pane:
                        break
                    time.sleep(0.1)
                self.assertIn("fake prompt accepted", pane)
                result, _, _ = watchdog.handle_capacity(args, meta, target, pane, 102)
                self.assertEqual(result["health_status"], "active")
                self.assertEqual(result["capacity_retry_count"], 0)
            finally:
                subprocess.run(["tmux", "-S", socket, "kill-server"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_nested_hook_success_cannot_override_top_level_failure(self):
        with tempfile.TemporaryDirectory(prefix="hook-") as directory:
            root = Path(directory)
            runtime = self.isolated_runtime(root)
            session = root / "session"
            session.mkdir()
            meta = session / "task-meta.json"
            meta.write_text(json.dumps({"task_id": "nested-test", "status": "running", "feishu_target": ""}))
            payload = json.dumps({"type": "agent-turn-complete", "thread-id": "fixture", "turn-id": "fixture",
                "last-assistant-message": json.dumps({"ok": False, "status": "failed", "child": {"ok": True, "status": "success"}})})
            subprocess.run(["bash", str(runtime / "hooks/notify-agi.sh"), payload],
                env={**os.environ, "CODING_AGENT_TASK_ID": "nested-test", "CODING_AGENT_SESSION_DIR": str(session)},
                check=True, capture_output=True)
            self.assertEqual(json.loads(meta.read_text())["status"], "failed")

    def run_worker(self, *, update_once: bool) -> tuple[subprocess.CompletedProcess[str], Path, str, Path]:
        temp_dir = Path(tempfile.mkdtemp(prefix="codex-dispatch-test-"))
        runtime = self.isolated_runtime(temp_dir)
        socket_dir = temp_dir / "socket"
        session_dir = temp_dir / "session"
        session_dir.mkdir()
        session = "health-test"
        update_marker = temp_dir / "updated.marker"
        task_id = "health-test-task"
        (session_dir / "task-meta.json").write_text(
            json.dumps(
                {
                    "task_name": session,
                    "task_id": task_id,
                    "status": "starting",
                    "lifecycle_stage": "dispatch_started",
                    "started_at": "2020-01-01T00:00:00Z",
                }
            )
        )
        env = os.environ.copy()
        env.update(
            {
                "CODEX_DISPATCH_WATCHDOG": "0",
                "SESSION_TTL": "0",
                "FAKE_CODEX_UPDATE_ONCE": str(update_marker) if update_once else "",
                "CODING_AGENT_TASK_ID": task_id,
                "CODING_AGENT_SESSION_DIR": str(session_dir),
                "HOME": os.environ.get("HOME", "/root"),
            }
        )
        command = [
            *self.isolated_runner(runtime),
            "--mode",
            "tmux",
            "--codex-bin",
            str(FAKE_CODEX),
            "--cwd",
            str(temp_dir),
            "--tmux-session",
            session,
            "--tmux-socket-dir",
            str(socket_dir),
            "-p",
            "health gate test",
        ]
        try:
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=45)
        finally:
            subprocess.run(
                ["tmux", "-S", str(socket_dir / "codex-code.sock"), "kill-server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        return result, session_dir, session, update_marker

    def test_startup_gate_retries_once_after_update_message(self) -> None:
        result, session_dir, _, marker = self.run_worker(update_once=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.exists())
        self.assertIn("retrying once", result.stdout)
        self.assertNotIn("startup health gate failed", result.stderr)
        state = json.loads((session_dir / "task-meta.json").read_text())
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["lifecycle_stage"], "business_started")
        self.assertTrue(state["update_restart_attempted"])

    def test_startup_gate_rejects_missing_worker(self) -> None:
        temp_dir = Path(tempfile.mkdtemp(prefix="codex-dispatch-test-"))
        runtime = self.isolated_runtime(temp_dir)
        socket_path = temp_dir / "socket" / "codex-code.sock"
        result = subprocess.run(
            [
                *self.isolated_runner(runtime),
                "--mode",
                "tmux",
                "--codex-bin",
                "/bin/true",
                "--cwd",
                str(temp_dir),
                "--tmux-session",
                "missing-worker",
                "--tmux-socket-dir",
                str(temp_dir / "socket"),
                "-p",
                "health gate must fail",
            ],
            env={**os.environ, "CODEX_DISPATCH_WATCHDOG": "0", "SESSION_TTL": "0"},
            text=True,
            capture_output=True,
            timeout=45,
        )
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("startup health gate failed", result.stderr)

    def test_update_check_is_disabled_by_default(self) -> None:
        sys_path = str(ROOT)
        code = (
            "import argparse,sys; sys.path.insert(0, %r); "
            "import codex_run; a=argparse.Namespace(allow_update_check=False); "
            "print(codex_run.codex_update_check_args(a))"
        ) % sys_path
        result = subprocess.run(["python3", "-c", code], text=True, capture_output=True, check=True)
        self.assertIn("check_for_update_on_startup=false", result.stdout)

    def test_watchdog_marks_missing_session_failed(self) -> None:
        temp_dir = Path(tempfile.mkdtemp(prefix="codex-dispatch-watchdog-"))
        runtime = self.isolated_runtime(temp_dir)
        session_dir = temp_dir / "session"
        session_dir.mkdir()
        task_id = "watchdog-test"
        (session_dir / "task-meta.json").write_text(
            json.dumps(
                {
                    "task_name": "watchdog-test",
                    "task_id": task_id,
                    "status": "starting",
                    "lifecycle_stage": "worker_starting",
                    "started_at": "2020-01-01T00:00:00Z",
                    "startup_started_at": "2020-01-01T00:00:00Z",
                    "feishu_target": "",
                }
            )
        )
        result = subprocess.run(
            [
                "python3",
                str(runtime / "watchdog.py"),
                "--session-dir",
                str(session_dir),
                "--task-id",
                task_id,
                "--socket",
                str(temp_dir / "missing.sock"),
                "--tmux-session",
                "missing-session",
                "--once",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(result.returncode, 0)
        state = json.loads((session_dir / "task-meta.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["lifecycle_stage"], "worker_failed")

    def test_watchdog_requires_a_real_model_response_after_capacity_prompt(self) -> None:
        args = argparse.Namespace(
            socket="/tmp/test-codex.sock",
            capacity_retries=2,
            capacity_delay_s=1,
            capacity_max_delay_s=4,
            capacity_response_timeout_s=2,
            capacity_input="Continue the original task from the current recovery point.",
        )
        snapshot = "Selected model is at capacity. Please try a different model."
        meta = {"status": "running"}
        with patch.object(watchdog, "send_capacity_input") as send:
            first, done, warning = watchdog.handle_capacity(args, meta, "task:0.0", snapshot, current_time=100.0)
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(first["capacity_retry_count"], 1)
            self.assertEqual(first["health_status"], "capacity_waiting_response")
            self.assertTrue(first["capacity_retry_pending"])
            send.assert_called_once_with(args.socket, "task:0.0", args.capacity_input)
            meta.update(first)

            waiting, done, warning = watchdog.handle_capacity(args, meta, "task:0.0", snapshot, current_time=100.5)
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(waiting["health_status"], "capacity_waiting_response")
            self.assertEqual(send.call_count, 1)
            meta.update(waiting)

            response_snapshot = snapshot + "\n• Working (1s • esc to interrupt)"
            response, done, warning = watchdog.handle_capacity(
                args, meta, "task:0.0", response_snapshot, current_time=101.1
            )
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(response["health_status"], "active")
            self.assertFalse(response["capacity_retry_pending"])
            self.assertEqual(response["capacity_retry_count"], 0)
            self.assertEqual(send.call_count, 1)
            meta.update(response)

            second, done, warning = watchdog.handle_capacity(
                args, meta, "task:0.0", snapshot, current_time=101.2
            )
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(second["capacity_retry_count"], 1)
            self.assertTrue(second["capacity_retry_pending"])
            self.assertEqual(send.call_count, 2)

    def test_watchdog_does_not_consume_next_retry_before_response_timeout(self) -> None:
        args = argparse.Namespace(
            socket="/tmp/test-codex.sock",
            capacity_retries=2,
            capacity_delay_s=1,
            capacity_max_delay_s=4,
            capacity_response_timeout_s=1,
            capacity_input="Continue the original task from the current recovery point.",
        )
        snapshot = "Selected model is at capacity. Please try a different model."
        meta = {"status": "running"}
        with patch.object(watchdog, "send_capacity_input") as send:
            first, done, warning = watchdog.handle_capacity(args, meta, "task:0.0", snapshot, current_time=100.0)
            self.assertFalse(done)
            self.assertIsNone(warning)
            meta.update(first)

            waiting, done, warning = watchdog.handle_capacity(args, meta, "task:0.0", snapshot, current_time=100.5)
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(send.call_count, 1)
            meta.update(waiting)

            no_response, done, warning = watchdog.handle_capacity(
                args, meta, "task:0.0", snapshot, current_time=101.1
            )
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(no_response["health_status"], "capacity_retry_no_response")
            self.assertFalse(no_response["capacity_retry_pending"])
            meta.update(no_response)

            before_next, done, warning = watchdog.handle_capacity(
                args, meta, "task:0.0", snapshot, current_time=101.5
            )
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(send.call_count, 1)
            meta.update(before_next)

            second, done, warning = watchdog.handle_capacity(args, meta, "task:0.0", snapshot, current_time=102.2)
            self.assertFalse(done)
            self.assertIsNone(warning)
            self.assertEqual(second["capacity_retry_count"], 2)
            self.assertTrue(second["capacity_retry_pending"])
            self.assertEqual(send.call_count, 2)

            meta.update(second)
            timed_out, done, warning = watchdog.handle_capacity(
                args, meta, "task:0.0", snapshot, current_time=103.3
            )
            self.assertFalse(done)
            self.assertIsNone(warning)
            meta.update(timed_out)

            exhausted, done, warning = watchdog.handle_capacity(args, meta, "task:0.0", snapshot, current_time=104.4)
            self.assertTrue(done)
            self.assertEqual(exhausted["status"], "waiting_input")
            self.assertEqual(exhausted["lifecycle_stage"], "capacity_blocked")
            self.assertIsNotNone(warning)
            self.assertEqual(send.call_count, 2)

    def test_watchdog_capacity_detection_requires_retry_guidance(self) -> None:
        self.assertFalse(watchdog.model_capacity_blocked("The task has capacity constraints."))
        self.assertTrue(watchdog.model_capacity_blocked(
            "Selected model is at capacity; please try a different model."
        ))
        self.assertFalse(watchdog.model_capacity_blocked(
            "Selected model is at capacity; please try a different model.\n"
            "• Working (2s • esc to interrupt)"
        ))
        self.assertEqual(watchdog.capacity_retry_delay(0, 30, 300), 30)
        self.assertEqual(watchdog.capacity_retry_delay(1, 30, 300), 60)
        self.assertEqual(watchdog.capacity_retry_delay(5, 30, 90), 90)

    def test_dispatch_wires_capacity_retry_settings_into_watchdog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-dispatch-capacity-") as temporary:
            session_dir = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "CODING_AGENT_SESSION_DIR": str(session_dir),
                    "CODING_AGENT_TASK_ID": "capacity-task",
                    "CODEX_DISPATCH_CAPACITY_RETRIES": "4",
                    "CODEX_DISPATCH_CAPACITY_DELAY_S": "7",
                    "CODEX_DISPATCH_CAPACITY_MAX_DELAY_S": "55",
                    "CODEX_DISPATCH_CAPACITY_INPUT": "Continue the original task from the current recovery point.",
                    "CODEX_DISPATCH_CAPACITY_RESPONSE_TIMEOUT_S": "11",
                },
                clear=False,
            ), patch.object(codex_run.subprocess, "Popen") as popen:
                popen.return_value.pid = 321
                result = codex_run.start_watchdog("/tmp/capacity.sock", "capacity-session")
            self.assertEqual(result, 321)
            command = popen.call_args.args[0]
            self.assertIn("--capacity-retries", command)
            self.assertEqual(command[command.index("--capacity-retries") + 1], "4")
            self.assertEqual(command[command.index("--capacity-delay-s") + 1], "7")
            self.assertEqual(command[command.index("--capacity-max-delay-s") + 1], "55")
            self.assertEqual(
                command[command.index("--capacity-input") + 1],
                "Continue the original task from the current recovery point.",
            )
            self.assertEqual(command[command.index("--capacity-response-timeout-s") + 1], "11")

    def test_successful_aggregate_does_not_fail_on_skipped_child_exit_code(self) -> None:
        temp_dir = Path(tempfile.mkdtemp(prefix="codex-dispatch-hook-"))
        runtime = self.isolated_runtime(temp_dir)
        session_dir = temp_dir / "session"
        session_dir.mkdir()
        task_id = "aggregate-success-task"
        (session_dir / "task-meta.json").write_text(
            json.dumps(
                {
                    "task_name": "aggregate-success",
                    "task_id": task_id,
                    "status": "running",
                    "lifecycle_stage": "business_started",
                    "feishu_target": "",
                }
            )
        )
        payload = json.dumps(
            {
                "type": "agent-turn-complete",
                "thread-id": "hook-test-thread",
                "turn-id": "hook-test-turn",
                "cwd": "/root",
                "last-assistant-message": json.dumps(
                    {
                        "ok": True,
                        "status": "success",
                        "failed_count": 0,
                        "sites": [{"status": "skipped", "exit_code": 1}],
                    }
                ),
            }
        )
        env = {
            **os.environ,
            "CODING_AGENT_TASK_ID": task_id,
            "CODING_AGENT_SESSION_DIR": str(session_dir),
            "FEISHU_TARGET": "",
        }
        result = subprocess.run(
            ["bash", str(runtime / "hooks/notify-agi.sh"), payload],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(result.returncode, 0)
        state = json.loads((session_dir / "task-meta.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["lifecycle_stage"], "completed")


if __name__ == "__main__":
    unittest.main()
