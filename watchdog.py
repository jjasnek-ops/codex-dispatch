#!/usr/bin/env python3
"""Health watchdog for Codex dispatch tmux workers.

The watchdog never kills or restarts a business task. It distinguishes a
missing worker from a merely quiet pane, records the state transition, and
emits one failure/warning notification when a task becomes unhealthy. A
bounded model-capacity retry submits an explicit continuation prompt to the
existing Codex session and waits for a real work marker; it never changes
models, restarts the worker, or repeats business stages itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from codex_run import (
    find_codex_worker,
    snapshot_digest,
    tmux_capture,
    tmux_cmd,
)


SCRIPT_DIR = Path(__file__).parent
NOTIFY_SCRIPT = SCRIPT_DIR / "hooks" / "notify-agi.sh"
TERMINAL_STATUSES = {"done", "failed", "error", "waiting_input"}
CAPACITY_MARKER = "model is at capacity"
CAPACITY_RETRY_MARKER = "try a different model"
DEFAULT_CAPACITY_INPUT = (
    "Continue the original task from the current recovery point; "
    "reuse completed evidence and do not repeat completed stages."
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch(value: str | None) -> float:
    if not value:
        return time.time()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def load_meta(session_dir: Path) -> dict:
    path = session_dir / "task-meta.json"
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_state(session_dir: Path, task_id: str, **updates: object) -> dict:
    result_dir = SCRIPT_DIR / "data"
    paths = [session_dir / "task-meta.json", result_dir / "tasks" / f"{task_id}.json"]
    current: dict = {}
    seen: set[str] = set()
    for path in paths:
        if str(path) in seen or not path.exists():
            continue
        seen.add(str(path))
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        data.update(updates)
        atomic_json(path, data)
        current = data
    return current


def has_session(socket_path: str, session: str) -> bool:
    return subprocess.run(
        tmux_cmd(socket_path, "has-session", "-t", session),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def notify_failure(session_dir: Path, task_id: str, reason: str) -> None:
    if not NOTIFY_SCRIPT.exists():
        return
    meta = load_meta(session_dir)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "CODING_AGENT_TASK_ID": task_id,
        "CODING_AGENT_SESSION_DIR": str(session_dir),
        "CODEX_EXIT_CODE": "1",
        "CODEX_FAILURE_REASON": reason,
        "CODEX_TASK_OUTPUT": str(session_dir / "task-output.txt"),
    }
    try:
        subprocess.run(
            [str(NOTIFY_SCRIPT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def notify_warning(meta: dict, reason: str) -> None:
    target = str(meta.get("feishu_target") or "").strip()
    if not target:
        return
    message = (
        "⚠️ Codex 任务健康告警\n"
        f"📋 任务: {meta.get('task_name', 'unknown')}\n"
        f"📝 事件: {reason}\n"
        "任务未被自动终止，请检查 tmux 与业务阶段证据。"
    )
    try:
        subprocess.run(
            [
                "/usr/bin/timeout",
                "8",
                "/usr/bin/openclaw",
                "message",
                "send",
                "--channel",
                "feishu",
                "--target",
                target,
                "--message",
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def capacity_index(snapshot: str) -> int:
    lines = snapshot.lower().splitlines()
    indexes = []
    for index, line in enumerate(lines):
        # Codex may wrap guidance onto the following line. Never combine an
        # arbitrary historical error with guidance far away in the transcript.
        if re.match(r"^\s*[■✕!]?\s*selected model is at capacity\b", line):
            if CAPACITY_RETRY_MARKER in " ".join(lines[index:index + 3]):
                indexes.append(index)
    return max(indexes, default=-1)


def work_markers(snapshot: str) -> list[tuple[int, str]]:
    markers = []
    for index, line in enumerate(snapshot.splitlines()):
        if re.match(r"^\s*[•◦]\s+(?:working\b|.+\([^)]*esc to interrupt\))", line, re.I):
            # A ticking elapsed-time counter is not a new model turn.
            stable = re.sub(r"\([^)]*\)", "", line).strip().lower()
            markers.append((index, snapshot_digest(stable)))
    return markers


def model_capacity_blocked(snapshot: str) -> bool:
    last_capacity = capacity_index(snapshot)
    return last_capacity >= 0 and max((i for i, _ in work_markers(snapshot)), default=-1) <= last_capacity


def model_capacity_response_seen(snapshot: str, baseline: list[str] | None = None) -> bool:
    """Require a positive, fresh marker, not a cleared error or input echo."""
    if baseline is None:  # Old pending metadata has no trustworthy send baseline.
        return False
    markers = work_markers(snapshot)
    if not markers or markers[-1][0] <= capacity_index(snapshot):
        return False
    latest = markers[-1][1]
    return sum(digest == latest for _, digest in markers) > baseline.count(latest)


def capacity_retry_delay(retry_count: int, base_s: int, max_s: int) -> int:
    """Compute a bounded exponential delay before the next capacity retry."""
    base = max(1, int(base_s))
    ceiling = max(base, int(max_s))
    return min(ceiling, base * (2 ** max(0, retry_count)))


def send_capacity_input(socket_path: str, target: str, value: str) -> None:
    """Submit one safe, literal input line to the existing tmux pane."""
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("capacity retry input must be one non-empty line")
    subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "-l", "--", value))
    # Allow the TUI paste/input handler to settle before a separate submit key.
    time.sleep(0.2)
    subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))


def handle_capacity(
    args: argparse.Namespace,
    meta: dict,
    target: str,
    snapshot: str,
    current_time: float | None = None,
) -> tuple[dict[str, object], bool, str | None]:
    """Return metadata updates, terminal flag, and an optional warning.

    Send an explicit continuation prompt, then wait for a real Codex work
    marker before treating the retry as accepted. A successful tmux write is
    not proof that the model accepted the turn. Further attempts use bounded
    exponential backoff. Once the retry budget is spent, the task becomes
    ``waiting_input`` so the session and evidence remain available for a
    human/model choice without being reported as successful.
    """
    now = time.time() if current_time is None else current_time
    pending = bool(meta.get("capacity_retry_pending"))
    blocked = model_capacity_blocked(snapshot)

    if pending or meta.get("capacity_retry_count"):
        turn_at = str(meta.get("last_turn_completed_at") or "")
        hook_response = (bool(turn_at) and bool(meta.get("last_turn_thread_id"))
                         and turn_at != meta.get("capacity_retry_baseline_turn_at")
                         and epoch(turn_at) >= epoch(meta.get("capacity_retry_sent_at")))
        if not blocked and (model_capacity_response_seen(snapshot, meta.get("capacity_retry_baseline_work")) or hook_response):
            return {
                "capacity_retry_pending": False,
                "capacity_retry_count": 0,
                "capacity_retry_marker_digest": None,
                "capacity_retry_response_at": epoch_iso(now),
                "capacity_retry_next_at": None,
                "capacity_retry_last_error": None,
                "capacity_retry_exhausted_notified": False,
                "health_status": "active",
                "last_health_event": "model_capacity_retry_response_received",
            }, False, None

    if pending:
        response_timeout = max(1, int(getattr(args, "capacity_response_timeout_s", 30)))
        sent_at = epoch(str(meta.get("capacity_retry_sent_at") or ""))
        if now < sent_at + response_timeout:
            return {
                "health_status": "capacity_waiting_response",
                "last_health_event": "waiting_for_model_capacity_response",
            }, False, None

        # The prompt was written to tmux but no real model response arrived.
        # Count send attempts separately from acknowledgements. Failed delivery
        # must remain bounded; it is never counted as a successful model retry.
        try:
            retry_count = max(0, int(meta.get("capacity_retry_count") or 0))
        except (TypeError, ValueError):
            retry_count = 0
        delay = capacity_retry_delay(max(0, retry_count - 1), args.capacity_delay_s, args.capacity_max_delay_s)
        next_at = now + delay
        return {
            "capacity_retry_pending": False,
            "capacity_retry_next_at": epoch_iso(next_at),
            "health_status": "capacity_retry_no_response",
            "last_health_event": "model_capacity_retry_no_response",
            "capacity_retry_last_error": "Codex did not produce a new model response",
        }, False, None

    try:
        retry_count = max(0, int(meta.get("capacity_retry_count") or 0))
    except (TypeError, ValueError):
        retry_count = 0
    try:
        retry_limit = max(0, int(args.capacity_retries))
    except (AttributeError, TypeError, ValueError):
        retry_limit = 3
    marker_digest = snapshot_digest(snapshot)
    next_at_value = str(meta.get("capacity_retry_next_at") or "").strip()
    next_at = epoch(next_at_value) if next_at_value else 0.0

    if retry_count >= retry_limit:
        reason = "Codex model capacity retry budget exhausted; task is waiting for input"
        updates: dict[str, object] = {
            "status": "waiting_input",
            "lifecycle_stage": "capacity_blocked",
            "health_status": "blocked",
            "failure_reason": reason,
            "last_health_event": reason,
            "capacity_retry_marker_digest": marker_digest,
            "capacity_retry_exhausted_notified": True,
            "completed_at": now_iso(),
        }
        warning = None if meta.get("capacity_retry_exhausted_notified") else reason
        return updates, True, warning

    if not blocked:
        # A vanished capacity prompt without an acknowledgement is ambiguous:
        # retain the failed-response state, and never type into an unknown UI.
        if retry_count:
            return {"health_status": "capacity_retry_unconfirmed",
                    "last_health_event": "model_capacity_retry_response_unconfirmed"}, False, None
        return {}, False, None

    if now < next_at:
        return {
            "health_status": "capacity_waiting",
            "last_health_event": "waiting_for_model_capacity_retry",
        }, False, None

    attempt = retry_count + 1
    try:
        send_capacity_input(args.socket, target, args.capacity_input)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        reason = f"model capacity retry input failed: {exc}"
        delay = capacity_retry_delay(retry_count, args.capacity_delay_s, args.capacity_max_delay_s)
        next_at = now + delay
        return {
            "capacity_retry_count": attempt,
            "capacity_retry_marker_digest": marker_digest,
            "capacity_retry_last_at": epoch_iso(now),
            "capacity_retry_next_at": epoch_iso(next_at),
            "capacity_retry_pending": False,
            "health_status": "capacity_retry_send_failed",
            "last_health_event": reason,
            "capacity_retry_last_error": str(exc),
        }, False, reason

    return {
        "capacity_retry_count": attempt,
        "capacity_retry_marker_digest": marker_digest,
        "capacity_retry_last_at": epoch_iso(now),
        "capacity_retry_next_at": None,
        "capacity_retry_sent_at": epoch_iso(now),
        "capacity_retry_baseline_work": [digest for _, digest in work_markers(snapshot)],
        "capacity_retry_baseline_turn_at": meta.get("last_turn_completed_at"),
        "capacity_retry_pending": True,
        "health_status": "capacity_waiting_response",
        "last_health_event": f"model_capacity_retry_{attempt}_sent",
        "capacity_retry_last_error": None,
    }, False, None


def check_once(args: argparse.Namespace, state: dict) -> tuple[dict, bool]:
    session_dir = Path(args.session_dir)
    meta = load_meta(session_dir)
    if not meta:
        print("watchdog: task metadata unavailable", file=sys.stderr, flush=True)
        return state, True

    status = str(meta.get("status") or "")
    if status in TERMINAL_STATUSES:
        return meta, True

    target = f"{args.tmux_session}:0.0"
    current_time = time.time()
    started_at = epoch(str(meta.get("startup_started_at") or meta.get("started_at") or ""))

    if not has_session(args.socket, args.tmux_session):
        reason = "tmux session ended before task completion"
        update_state(
            session_dir,
            args.task_id,
            status="failed",
            lifecycle_stage="worker_failed",
            failure_reason=reason,
            completed_at=now_iso(),
            last_health_event=reason,
        )
        notify_failure(session_dir, args.task_id, reason)
        return load_meta(session_dir), True

    worker = find_codex_worker(args.socket, target)
    if worker:
        pid, _ = worker
        updates: dict[str, object] = {
            "worker_pid": pid,
            "last_heartbeat_at": now_iso(),
            "watchdog_last_check_at": now_iso(),
            "worker_missing_since": None,
        }
        snapshot = ""
        try:
            snapshot = tmux_capture(args.socket, target, lines=240)
            digest = snapshot_digest(snapshot)
        except (subprocess.CalledProcessError, OSError):
            digest = ""
        previous_digest = str(meta.get("last_pane_digest") or "")
        last_change = epoch(str(meta.get("last_pane_change_at") or ""))
        if digest and digest != previous_digest:
            updates["last_pane_digest"] = digest
            updates["last_pane_change_at"] = now_iso()
            updates["health_status"] = "active"
        elif not last_change:
            updates["last_pane_change_at"] = now_iso()
        if status == "starting" and meta.get("lifecycle_stage") == "worker_starting":
            updates["lifecycle_stage"] = "worker_started"

        if meta.get("capacity_retry_pending") or meta.get("capacity_retry_count") or model_capacity_blocked(snapshot):
            capacity_updates, capacity_done, capacity_warning = handle_capacity(
                args, meta, target, snapshot, current_time=current_time
            )
            updates.update(capacity_updates)
            event = capacity_updates.get("last_health_event")
            if event and event != meta.get("last_health_event"):
                print(f"watchdog: {event}", flush=True)
            if capacity_warning:
                notify_warning(meta, capacity_warning)
            update_state(session_dir, args.task_id, **updates)
            return load_meta(session_dir), capacity_done

        elif current_time - last_change >= args.heartbeat_timeout:
            if meta.get("health_status") != "stalled":
                updates["health_status"] = "stalled"
                updates["stalled_since"] = meta.get("stalled_since") or now_iso()
                reason = f"tmux pane unchanged for at least {args.heartbeat_timeout}s"
                print(f"watchdog: {reason}", flush=True)
                notify_warning(meta, reason)
        update_state(session_dir, args.task_id, **updates)
        return load_meta(session_dir), False

    missing_since = epoch(str(meta.get("worker_missing_since") or ""))
    if not meta.get("worker_missing_since"):
        update_state(
            session_dir,
            args.task_id,
            worker_missing_since=now_iso(),
            watchdog_last_check_at=now_iso(),
        )
        return load_meta(session_dir), False

    if status == "starting" and current_time - started_at >= args.startup_timeout:
        reason = f"Codex worker did not survive startup gate within {args.startup_timeout}s"
        update_state(
            session_dir,
            args.task_id,
            status="failed",
            lifecycle_stage="startup_failed",
            failure_reason=reason,
            completed_at=now_iso(),
            last_health_event=reason,
        )
        notify_failure(session_dir, args.task_id, reason)
        return load_meta(session_dir), True

    if status == "running" and current_time - missing_since >= args.worker_missing_grace:
        reason = f"Codex worker disappeared for at least {args.worker_missing_grace}s"
        update_state(
            session_dir,
            args.task_id,
            status="failed",
            lifecycle_stage="worker_failed",
            failure_reason=reason,
            completed_at=now_iso(),
            last_health_event=reason,
        )
        notify_failure(session_dir, args.task_id, reason)
        return load_meta(session_dir), True

    update_state(session_dir, args.task_id, watchdog_last_check_at=now_iso())
    return load_meta(session_dir), False


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch a Codex dispatch worker")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--tmux-session", required=True)
    parser.add_argument("--startup-timeout", type=int, default=120)
    parser.add_argument("--heartbeat-timeout", type=int, default=900)
    parser.add_argument("--worker-missing-grace", type=int, default=30)
    parser.add_argument("--capacity-retries", type=int, default=3)
    parser.add_argument("--capacity-delay-s", type=int, default=30)
    parser.add_argument("--capacity-max-delay-s", type=int, default=300)
    parser.add_argument("--capacity-input", default=DEFAULT_CAPACITY_INPUT)
    parser.add_argument("--capacity-response-timeout-s", type=int, default=30)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    state: dict = {}
    while True:
        state, done = check_once(args, state)
        if done or args.once:
            return 0
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
