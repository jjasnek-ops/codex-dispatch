"""Fault/concurrency regressions; isolated state and transport only."""
import json
import argparse
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_state as S
import watchdog
import codex_run


def concurrent_update(root, session, key):
    S.update(session, "new-task", {key: True}, root=Path(root))


def concurrent_hook(session):
    S.hook(session, "new-task", "done", "fixture complete", 0, "fixture-thread", "final-turn")


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        (S.ROOT / "tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="state-transaction-", dir=S.ROOT / "tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        self.root_patch = patch.object(S, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        S.initialize(self.session, {"task_id": "new-task", "task_name": "test", "status": "running", "feishu_target": ""})

    def meta(self):
        return S.read(self.session / "task-meta.json")

    def test_old_hook_and_old_watchdog_cannot_write_new_generation(self):
        before = self.meta()
        with self.assertRaises(S.StaleTask):
            S.hook(self.session, "old-task", "done", "old output", 0)
        with self.assertRaises(S.StaleTask):
            S.update(self.session, "old-task", {"status": "failed"})
        self.assertEqual(self.meta(), before)

    def test_active_same_name_initialization_does_not_overwrite(self):
        before = self.meta()
        with self.assertRaisesRegex(ValueError, "active_task"):
            S.initialize(self.session, {"task_id": "replacement", "status": "starting"})
        self.assertEqual(self.meta(), before)

    def test_terminal_is_monotonic_and_mirrors_match(self):
        S.hook(self.session, "new-task", "done", "final", 0, "thread", "turn-2")
        before = self.meta()
        S.hook(self.session, "new-task", "running", "late progress", 0, "thread", "turn-1")
        S.update(self.session, "new-task", {"health_status": "active", "status": "running"})
        self.assertEqual(self.meta(), before)
        for path in (self.root / "data/latest.json", self.root / "data/tasks/new-task.json"):
            self.assertEqual(S.read(path), before)

    def test_duplicate_hook_turn_is_idempotent(self):
        S.hook(self.session, "new-task", "running", "progress", 0, "thread", "turn-1")
        before = self.meta()
        S.hook(self.session, "new-task", "running", "progress", 0, "thread", "turn-1")
        self.assertEqual(self.meta(), before)

    def test_wrong_thread_cannot_publish_terminal(self):
        S.hook(self.session, "new-task", "running", "progress", 0, "right-thread", "turn-1")
        with self.assertRaises(S.StaleTask):
            S.hook(self.session, "new-task", "done", "wrong", 0, "wrong-thread", "turn-2")
        self.assertEqual(self.meta()["status"], "running")

    def test_concurrent_writers_merge_instead_of_losing_fields(self):
        processes = [multiprocessing.Process(target=concurrent_update, args=(str(self.root), str(self.session), f"key-{i}")) for i in range(8)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        data = self.meta()
        self.assertTrue(all(data.get(f"key-{i}") for i in range(8)))
        self.assertEqual(data["revision"], 9)
        self.assertEqual(S.read(self.root / "data/tasks/new-task.json"), data)

    def test_parallel_terminal_hook_and_watchdog_updates_keep_one_terminal_revision(self):
        processes = [multiprocessing.Process(target=concurrent_update, args=(str(self.root), str(self.session), f"heartbeat-{i}")) for i in range(8)]
        processes.insert(4, multiprocessing.Process(target=concurrent_hook, args=(str(self.session),)))
        for process in processes:
            process.start()
        for process in processes:
            process.join(5)
            self.assertEqual(0, process.exitcode)
        data = self.meta()
        self.assertEqual("done", data["status"])
        for path in (self.root / "data/latest.json", self.root / "data/tasks/new-task.json"):
            self.assertEqual(data, S.read(path))

    def test_stale_nonterminal_fields_removed(self):
        S.update(self.session, "new-task", {"status": "waiting_input", "completed_at": "stale", "exit_code": 1})
        self.assertNotIn("completed_at", self.meta())
        self.assertNotIn("exit_code", self.meta())

    def test_notification_per_task_dedup_does_not_suppress_other_task(self):
        first = {**self.meta(), "feishu_target": "fixture-no-network"}
        second = {**first, "task_id": "other-task"}
        a = S.enqueue(first, "terminal-done", "first")
        b = S.enqueue(second, "terminal-done", "second")
        self.assertNotEqual(a, b)
        self.assertEqual(S.enqueue(first, "terminal-done", "duplicate"), a)
        with patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as transport:
            S.drain(a)
            S.drain(b)
            S.drain(a)
            self.assertEqual(transport.call_count, 2)
        self.assertEqual(S.read(a)["state"], "delivered")
        self.assertEqual(S.read(b)["state"], "delivered")

    def test_failed_notification_retries_bounded_without_false_delivered(self):
        path = S.enqueue({**self.meta(), "feishu_target": "fixture-no-network"}, "terminal-failed", "failed")
        with patch.object(S.time, "sleep"), patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 71)) as transport:
            S.drain(path)
            S.drain(path)
        data = S.read(path)
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["last_exit_code"], 71)
        self.assertNotIn("delivered_at", data)

    def test_transient_notification_failure_then_success(self):
        path = S.enqueue({**self.meta(), "feishu_target": "fixture-no-network"}, "terminal-done", "complete")
        with patch.object(S.time, "sleep"), patch.object(S.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 71), subprocess.CompletedProcess([], 0)]):
            S.drain(path)
        self.assertEqual(S.read(path)["attempts"], 2)
        self.assertEqual(S.read(path)["state"], "delivered")

    def test_crashed_mirror_write_reconciles_from_canonical(self):
        with patch.object(S, "mirror", side_effect=OSError("simulated crash after canonical commit")):
            with self.assertRaises(OSError):
                S.update(self.session, "new-task", {"status": "done"})
        S.update(self.session, "new-task", {"status": "running"})
        self.assertEqual(self.meta()["status"], "done")
        self.assertEqual(S.read(self.root / "data/tasks/new-task.json"), self.meta())

    def test_old_watchdog_does_not_inspect_or_send_to_new_task(self):
        import argparse
        args = argparse.Namespace(session_dir=str(self.session), task_id="old-task")
        with patch.object(watchdog, "has_session") as target:
            _, done = watchdog.check_once(args, {})
            self.assertTrue(done)
            target.assert_not_called()

    def test_failure_notification_inside_state_lock_is_reentrant(self):
        with S.lock(self.session), patch.object(S, "schedule_delivery") as schedule:
            watchdog.notify_failure(self.session, "new-task", "fixture worker failed")
        self.assertEqual("failed", self.meta()["status"])
        self.assertTrue(self.meta()["terminal_hook_recorded"])
        schedule.assert_called_once()

    def test_strict_driver_can_import_runner_by_absolute_file(self):
        path = Path(watchdog.__file__).with_name("codex_run.py")
        code = "import importlib.util; s=importlib.util.spec_from_file_location('runner', __import__('sys').argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)"
        result = subprocess.run([sys.executable, "-I", "-c", code, str(path)], cwd=self.root, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode())

    def test_real_tmux_existing_name_is_not_replaced_and_deadline_does_not_kill(self):
        socket = str(self.root / "isolation.sock")
        def tmux(*args):
            return subprocess.check_output(["tmux", "-S", socket, *args], stderr=subprocess.DEVNULL, text=True).strip()
        tmux("new-session", "-d", "-s", "fixture", "sleep 60")
        try:
            instance = tmux("display-message", "-p", "-t", "=fixture", "#{session_id}:#{session_created}")
            tmux("set-option", "-t", instance.split(":")[0], "@dispatch-task-id", "new-task")
            args = argparse.Namespace(cwd=str(self.root), tmux_socket_dir=str(self.root),
                tmux_socket_name="isolation.sock", tmux_session="fixture", prompt="must not reach a model")
            with patch.object(codex_run, "_ensure_directory_trusted") as trust:
                self.assertEqual(codex_run.STARTUP_GATE_EXIT_CODE, codex_run.run_tmux(args))
                trust.assert_not_called()
            self.assertEqual(instance, tmux("display-message", "-p", "-t", "=fixture", "#{session_id}:#{session_created}"))
            S.update(self.session, "new-task", {"tmux_instance": instance, "tmux_generation": "new-task",
                "session_deadline_epoch": time.time() - 1, "started_at": S.now()})
            check = argparse.Namespace(session_dir=str(self.session), task_id="new-task", socket=socket,
                tmux_session="fixture", heartbeat_timeout=900, worker_missing_grace=30, startup_timeout=120)
            with patch.object(watchdog, "SCRIPT_DIR", self.root), patch.object(watchdog, "find_codex_worker", return_value=None):
                watchdog.check_once(check, {})
            self.assertTrue(self.meta()["deadline_notified"])
            self.assertEqual(instance, tmux("display-message", "-p", "-t", "=fixture", "#{session_id}:#{session_created}"))
            # Reuse only our own isolated name. The old generation must stop
            # before checking/sending to the new target, even if its name agrees.
            tmux("new-session", "-d", "-s", "keep-server", "sleep 60")
            tmux("kill-session", "-t", "=fixture")
            tmux("new-session", "-d", "-s", "fixture", "sleep 60")
            replacement = tmux("display-message", "-p", "-t", "=fixture", "#{session_id}:#{session_created}")
            tmux("set-option", "-t", replacement.split(":")[0], "@dispatch-task-id", "replacement")
            before = self.meta()
            with patch.object(watchdog, "find_codex_worker") as worker:
                _, done = watchdog.check_once(check, {})
                self.assertTrue(done)
                worker.assert_not_called()
            self.assertEqual(before, self.meta())
            self.assertEqual(replacement, tmux("display-message", "-p", "-t", "=fixture", "#{session_id}:#{session_created}"))
        finally:
            subprocess.run(["tmux", "-S", socket, "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
