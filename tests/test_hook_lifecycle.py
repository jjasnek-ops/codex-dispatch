#!/usr/bin/env python3
"""Real local Hook/watchdog/tmux coordination; no model or outbound messages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import test_dispatch_health as helpers


def exercise_lifecycle(root):
    runtime = helpers.DispatchHealthTests().isolated_runtime(root)
    session = root / "session"
    session.mkdir()
    name = "hook-lifecycle-fixture"
    socket = str(root / "test.sock")
    meta_path = session / "task-meta.json"
    meta_path.write_text(json.dumps({"task_id": name, "task_name": name, "status": "running",
        "lifecycle_stage": "business_started", "feishu_target": ""}))
    env = {**os.environ, "CODING_AGENT_TASK_ID": name, "CODING_AGENT_SESSION_DIR": str(session),
           "CODEX_EXIT_CODE": "0", "CODEX_FAILURE_REASON": "", "FEISHU_TARGET": ""}
    result = {"synthetic": True, "business_sessions_modified": False}
    watcher = None
    turn_sequence = 0

    def hook(message):
        nonlocal turn_sequence
        turn_sequence += 1
        payload = json.dumps({"type": "agent-turn-complete", "thread-id": "fixture",
            "turn-id": f"fixture-turn-{turn_sequence}", "last-assistant-message": message})
        subprocess.run(["bash", str(runtime / "hooks/notify-agi.sh"), payload],
                       env=env, check=True, capture_output=True, timeout=5)

    def read():
        return json.loads(meta_path.read_text())

    try:
        # Keep a shell parent, so the unmodified worker-health check finds an
        # actual --no-daemon descendant, not a mocked PID or shell echo.
        subprocess.run(["tmux", "-S", socket, "new-session", "-d", "-s", name,
            "bash", "-c", '"$@"; wait', "fixture", sys.executable, str(helpers.FAKE_CODEX), "--no-daemon"],
            env={**env, "FAKE_CODEX_CAPACITY": "1", "FAKE_CODEX_UPDATE_ONCE": ""}, check=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pane = helpers.watchdog.tmux_capture(socket, name + ":0.0")
            if helpers.watchdog.model_capacity_blocked(pane):
                break
            time.sleep(0.1)
        assert helpers.watchdog.model_capacity_blocked(pane), "fixture failed to reach capacity"
        hook(json.dumps({"title": "checking Part 2"}, indent=2))
        assert read()["status"] == "running", "pretty progress stopped task"
        watcher = subprocess.Popen([sys.executable, str(runtime / "watchdog.py"), "--session-dir", str(session),
            "--task-id", name, "--socket", socket, "--tmux-session", name, "--interval", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            assert watcher.poll() is None, "watchdog exited during progress"
            if read().get("capacity_retry_response_at"):
                break
            time.sleep(0.1)
        state = read()
        pane = helpers.watchdog.tmux_capture(socket, name + ":0.0")
        result["actual_input_accepted"] = "fake prompt accepted" in pane
        result["capacity_response_confirmed"] = bool(state.get("capacity_retry_response_at"))
        assert result["actual_input_accepted"] and result["capacity_response_confirmed"], "no real retry response"
        assert state["capacity_retry_count"] == 0
        hook(json.dumps({"status": "running", "title": "Part 2 complete"}))
        try:
            watcher.communicate(timeout=2)
            raise AssertionError("watchdog stopped after running hook")
        except subprocess.TimeoutExpired:
            result["watchdog_active_after_progress"] = True
        for path in (meta_path, runtime / "data/tasks" / (name + ".json"), runtime / "data/latest.json"):
            state = json.loads(path.read_text())
            assert state["status"] == "running" and "completed_at" not in state and "exit_code" not in state
        result["all_progress_mirrors_consistent"] = True
        hook(json.dumps({"title": "final", "ok": True, "status": "success_with_warnings"}, indent=2))
        stdout, stderr = watcher.communicate(timeout=5)
        result["watchdog_exit_code"] = watcher.returncode
        result["terminal_status"] = read()["status"]
        assert watcher.returncode == 0 and result["terminal_status"] == "done", stderr
        result["tmux_alive_until_explicit_cleanup"] = subprocess.run(
            ["tmux", "-S", socket, "has-session", "-t", name], capture_output=True).returncode == 0
        assert result["tmux_alive_until_explicit_cleanup"]
    finally:
        if watcher and watcher.poll() is None:
            watcher.terminate()
            watcher.communicate(timeout=5)
        subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True, check=False)
        result["isolated_server_closed"] = subprocess.run(
            ["tmux", "-S", socket, "has-session", "-t", name], capture_output=True).returncode != 0
        (root / "lifecycle-result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


class HookLifecycleTests(unittest.TestCase):
    def test_real_watchdog_survives_progress_retries_capacity_and_stops_at_final(self):
        (helpers.ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hook-life-", dir=helpers.ROOT / "tmp") as directory:
            result = exercise_lifecycle(Path(directory))
            self.assertTrue(result["capacity_response_confirmed"])
            self.assertTrue(result["isolated_server_closed"])


if __name__ == "__main__":
    unittest.main()
