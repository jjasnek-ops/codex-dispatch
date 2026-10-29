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

    def test_title_and_actual_result_share_turn_without_losing_result(self):
        S.hook(self.session, "new-task", "running", '{"title":"smoke"}', 0, "thread", "turn-1")
        S.hook(self.session, "new-task", "waiting_input", '{"status":"waiting_input"}', 0, "thread", "turn-1")
        self.assertEqual(self.meta()["status"], "waiting_input")
        before = self.meta()
        S.hook(self.session, "new-task", "running", '{"title":"late title"}', 0, "thread", "turn-1")
        self.assertEqual(self.meta(), before)
        S.hook(self.session, "new-task", "running", '{"status":"running"}', 0, "thread", "turn-2")
        S.hook(self.session, "new-task", "done", '{"ok":true,"status":"success"}', 0, "thread", "turn-2")
        self.assertEqual(self.meta()["status"], "done")
        before = self.meta()
        S.hook(self.session, "new-task", "done", '{"ok":true,"status":"success"}', 0, "thread", "turn-2")
        self.assertEqual(self.meta(), before)

    def test_background_title_cannot_claim_or_conflict_with_real_thread(self):
        before = self.meta()
        S.hook(self.session, "new-task", "running", '{"title":"name"}', 0, "title-thread", "title-turn")
        self.assertEqual(before, self.meta())
        S.hook(self.session, "new-task", "waiting_input", '{"status":"waiting_input"}', 0, "real-thread", "real-turn")
        self.assertEqual(self.meta()["thread_id"], "real-thread")
        before = self.meta()
        S.hook(self.session, "new-task", "running", '{"title":"name"}', 0, "other-title-thread", "title-turn-2")
        self.assertEqual(before, self.meta())

    def test_late_old_turn_cannot_change_resumed_turn(self):
        S.hook(self.session, "new-task", "running", "first", 0, "thread", "turn-1")
        S.hook(self.session, "new-task", "running", "second", 0, "thread", "turn-2")
        before = self.meta()
        S.hook(self.session, "new-task", "waiting_input", "late first", 0, "thread", "turn-1")
        self.assertEqual(self.meta(), before)

    def test_legacy_turn_id_only_progress_can_accept_real_final(self):
        S.update(self.session, "new-task", {"hook_turn_ids": ["turn-1"], "thread_id": "thread", "output": '{"title":"old"}'})
        S.hook(self.session, "new-task", "done", '{"ok":true,"status":"success"}', 0, "thread", "turn-1")
        self.assertEqual(self.meta()["status"], "done")

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

    def test_notification_budget_covers_measured_cli_startup_and_keeps_channel_id(self):
        path = S.enqueue({**self.meta(), "feishu_target": "fixture-no-network"}, "terminal-done", "complete")
        result = subprocess.CompletedProcess([], 0, stdout='{"result":{"messageId":"om_fixture_real_id"}}')
        with patch.object(S.subprocess, "run", return_value=result) as transport:
            S.drain(path)
        command = transport.call_args.args[0]
        self.assertEqual(command[2], "45")
        self.assertIn("--kill-after=5", command)
        self.assertEqual(transport.call_args.kwargs["timeout"], 52)
        self.assertEqual(S.read(path)["transport_message_id"], "om_fixture_real_id")
        self.assertEqual(S.read(path)["attempts"], 1)

    def test_crashed_mirror_write_reconciles_from_canonical(self):
        with patch.object(S, "mirror", side_effect=OSError("simulated crash after canonical commit")):
            with self.assertRaises(OSError):
                S.update(self.session, "new-task", {"status": "done"})
        S.update(self.session, "new-task", {"status": "running"})
        self.assertEqual(self.meta()["status"], "done")
        self.assertEqual(S.read(self.root / "data/tasks/new-task.json"), self.meta())

    def watchdog_args(self):
        return argparse.Namespace(session_dir=str(self.session), task_id="new-task", socket="unused",
            tmux_session="fixture", heartbeat_timeout=900, worker_missing_grace=30, startup_timeout=120)

    def test_watchdog_recovers_terminal_mirrors_without_another_hook(self):
        with patch.object(S, "mirror", side_effect=OSError("canonical commit survived")):
            with self.assertRaises(OSError):
                S.update(self.session, "new-task", {"status": "done"})
        self.assertEqual("running", S.read(self.root / "data/tasks/new-task.json")["status"])
        with patch.object(watchdog, "SCRIPT_DIR", self.root), patch.object(watchdog, "has_session", return_value=False):
            _, stopped = watchdog.check_once(self.watchdog_args(), {})
        self.assertTrue(stopped)
        self.assertEqual(self.meta(), S.read(self.root / "data/tasks/new-task.json"))
        self.assertEqual(self.meta(), S.read(self.root / "data/latest.json"))

    def test_failure_commit_before_hook_is_deliverable_after_restart(self):
        S.update(self.session, "new-task", {"feishu_target": "fixture-no-network"})
        with patch.object(watchdog, "SCRIPT_DIR", self.root), patch.object(watchdog, "has_session", return_value=False):
            with patch.object(watchdog, "notify_failure", side_effect=RuntimeError("process death before Hook")):
                with self.assertRaises(RuntimeError):
                    watchdog.check_once(self.watchdog_args(), {})
            self.assertEqual("failed", self.meta()["status"])
            with patch.object(S, "schedule_delivery") as deliver:
                _, stopped = watchdog.check_once(self.watchdog_args(), {})
                self.assertTrue(stopped)
                deliver.assert_called_once()
                receipt = Path(deliver.call_args.args[0])
        self.assertEqual("pending", S.read(receipt)["state"])
        self.assertIn("tmux session ended", S.read(receipt)["message"])

    def test_committed_intent_survives_outbox_write_crash_and_preserves_budget(self):
        S.update(self.session, "new-task", {"feishu_target": "fixture-no-network"})
        original = S.atomic_json
        def write(path, value):
            if Path(path).parent.name == "outbox":
                raise OSError("interrupted outbox materialization")
            original(path, value)
        with patch.object(S, "atomic_json", side_effect=write):
            with self.assertRaises(OSError):
                S.hook(self.session, "new-task", "done", "fixture final", 0)
        self.assertEqual("done", self.meta()["status"])
        self.assertTrue(self.meta()["notification_events"])
        self.assertFalse(Path(self.meta()["notification_receipt"]).exists())
        with patch.object(watchdog, "SCRIPT_DIR", self.root), patch.object(watchdog, "has_session", return_value=False), \
                patch.object(S, "schedule_delivery") as deliver:
            watchdog.check_once(self.watchdog_args(), {})
            deliver.assert_called_once()
        path = Path(self.meta()["notification_receipt"])
        item = S.read(path)
        item.update(attempts=2, state="pending", last_exit_code=71)
        S.atomic_json(path, item)
        S.reconcile(self.session, "new-task")
        self.assertEqual(item, S.read(path))
        with patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 71)) as send:
            S.drain(path)
        self.assertEqual(1, send.call_count)
        self.assertEqual(3, S.read(path)["attempts"])
        self.assertEqual([], S.pending_receipts(self.meta()))

    def test_uncommitted_terminal_intent_never_creates_a_sendable_receipt(self):
        S.update(self.session, "new-task", {"feishu_target": "fixture-no-network"})
        original = S.atomic_json
        def write(path, value):
            if Path(path).name == "task-meta.json":
                raise OSError("canonical commit failed")
            original(path, value)
        with patch.object(S, "atomic_json", side_effect=write), self.assertRaises(OSError):
            S.hook(self.session, "new-task", "done", "never committed", 0)
        self.assertEqual("running", self.meta()["status"])
        self.assertEqual([], list((self.root / "data/outbox").glob("*.json")))

    def test_startup_and_health_intents_are_reconciled_without_budget_reset(self):
        S.update(self.session, "new-task", {"feishu_target": "fixture-no-network"})
        with patch.object(S, "reconcile_derived", side_effect=OSError("crash after state")):
            with self.assertRaises(OSError):
                S.queue_notification(self.session, "new-task", "startup", "fixture startup")
        self.assertEqual("running", self.meta()["status"])
        S.reconcile(self.session, "new-task")
        paths = S.pending_receipts(self.meta())
        self.assertEqual(1, len(paths))
        with patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            S.drain(paths[0])
        S.queue_notification(self.session, "new-task", "startup", "duplicate")
        self.assertEqual([], S.pending_receipts(self.meta()))
        self.assertEqual("fixture startup", S.read(paths[0])["message"])
        self.assertEqual(1, S.read(paths[0])["attempts"])

    def test_waiting_input_parks_watchdog_and_running_hook_resumes_checks(self):
        S.update(self.session, "new-task", {"capacity_retry_count": 3})
        S.hook(self.session, "new-task", "waiting_input", "need user", 0, "thread", "turn-1")
        with patch.object(watchdog, "SCRIPT_DIR", self.root), patch.object(watchdog, "has_session", return_value=True), \
                patch.object(watchdog, "find_codex_worker", return_value=None) as worker, \
                patch.object(watchdog, "send_capacity_input") as send:
            _, stopped = watchdog.check_once(self.watchdog_args(), {})
            self.assertFalse(stopped)
            worker.assert_not_called()
            send.assert_not_called()
            self.assertEqual(3, self.meta()["capacity_retry_count"])
            S.hook(self.session, "new-task", "running", "user resumed", 0, "thread", "turn-2")
            _, stopped = watchdog.check_once(self.watchdog_args(), {})
            self.assertFalse(stopped)
            worker.assert_called_once()

    def test_deadline_crash_cannot_set_suppression_before_durable_notification(self):
        S.update(self.session, "new-task", {"session_deadline_epoch": time.time() - 1,
            "started_at": S.now(), "feishu_target": "fixture-no-network"})
        original = watchdog.update_state
        def update(session, task, **updates):
            if updates.get("deadline_notified"):
                raise RuntimeError("process died before suppression flag")
            return original(session, task, **updates)
        with patch.object(watchdog, "SCRIPT_DIR", self.root), patch.object(watchdog, "has_session", return_value=True), \
                patch.object(watchdog, "find_codex_worker", return_value=None), patch.object(S, "schedule_delivery"):
            with patch.object(watchdog, "update_state", side_effect=update), self.assertRaises(RuntimeError):
                watchdog.check_once(self.watchdog_args(), {})
            self.assertEqual(1, len(self.meta()["notification_events"]))
            self.assertFalse(self.meta().get("deadline_notified"))
            watchdog.check_once(self.watchdog_args(), {})
        self.assertTrue(self.meta()["deadline_notified"])
        self.assertEqual(1, len(self.meta()["notification_events"]))

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
            instance = codex_run.tmux_session_instance(socket, "fixture")
            self.assertRegex(instance, r"^\$\d+:[1-9]\d*$")
            tmux("set-option", "-t", instance.split(":")[0], "@dispatch-task-id", "new-task")
            args = argparse.Namespace(cwd=str(self.root), tmux_socket_dir=str(self.root),
                tmux_socket_name="isolation.sock", tmux_session="fixture", prompt="must not reach a model")
            with patch.object(codex_run, "_ensure_directory_trusted") as trust:
                self.assertEqual(codex_run.STARTUP_GATE_EXIT_CODE, codex_run.run_tmux(args))
                trust.assert_not_called()
            self.assertEqual(instance, codex_run.tmux_session_instance(socket, "fixture"))
            S.update(self.session, "new-task", {"tmux_instance": instance, "tmux_generation": "new-task",
                "session_deadline_epoch": time.time() - 1, "started_at": S.now()})
            check = argparse.Namespace(session_dir=str(self.session), task_id="new-task", socket=socket,
                tmux_session="fixture", heartbeat_timeout=900, worker_missing_grace=30, startup_timeout=120)
            with patch.object(watchdog, "SCRIPT_DIR", self.root), patch.object(watchdog, "find_codex_worker", return_value=None):
                watchdog.check_once(check, {})
            self.assertTrue(self.meta()["deadline_notified"])
            self.assertEqual(instance, codex_run.tmux_session_instance(socket, "fixture"))
            # Reuse only our own isolated name. The old generation must stop
            # before checking/sending to the new target, even if its name agrees.
            tmux("new-session", "-d", "-s", "keep-server", "sleep 60")
            tmux("kill-session", "-t", "=fixture")
            tmux("new-session", "-d", "-s", "fixture", "sleep 60")
            replacement = codex_run.tmux_session_instance(socket, "fixture")
            self.assertNotEqual(instance, replacement)
            tmux("set-option", "-t", replacement.split(":")[0], "@dispatch-task-id", "replacement")
            before = self.meta()
            with patch.object(watchdog, "find_codex_worker") as worker:
                _, done = watchdog.check_once(check, {})
                self.assertTrue(done)
                worker.assert_not_called()
            self.assertEqual(before, self.meta())
            self.assertEqual(replacement, codex_run.tmux_session_instance(socket, "fixture"))
        finally:
            subprocess.run(["tmux", "-S", socket, "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
