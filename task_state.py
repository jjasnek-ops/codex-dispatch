#!/usr/bin/env python3
"""Generation-fenced task state and a per-event, bounded notification outbox.

task-meta.json is authoritative. Mirrors carry its revision and are reconciled
under the same lock. No operation here starts a business task or repairs it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent
TERMINAL = {"done", "failed", "error"}
_local = threading.local()
_thread_lock = threading.RLock()


class StaleTask(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("state_object_required")
    return value


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def lock(session_dir):
    """Reentrant within a thread, serialized across threads and processes."""
    path = Path(session_dir).resolve() / ".task-state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock:
        held = getattr(_local, "held", set())
        if path in held:
            yield
            return
        with path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _local.held = held | {path}
            try:
                yield
            finally:
                _local.held = held
                fcntl.flock(handle, fcntl.LOCK_UN)


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\w.-]+", value) or value in {".", ".."}:
        raise ValueError("invalid_task_id")
    return value


def current(session_dir, task_id):
    meta = read(Path(session_dir) / "task-meta.json")
    if meta.get("task_id") != identity(task_id):
        raise StaleTask("stale_task_generation")
    return meta


def mirror(session_dir, data, root=None):
    root = Path(root or ROOT)
    atomic_json(root / "data/tasks" / (identity(data["task_id"]) + ".json"), data)
    atomic_json(root / "data/latest.json", data)


def notification_key(task_id, kind, target):
    return hashlib.sha256(json.dumps([task_id, kind, "feishu", target]).encode()).hexdigest()


def remember_notification(data, kind, message, root=None):
    """Record an immutable intent in the canonical transaction before I/O."""
    if not data.get("feishu_target"):
        return None
    key = notification_key(data["task_id"], kind, data["feishu_target"])
    intents = data.setdefault("notification_events", {})
    if key not in intents:
        intents[key] = {"task_id": data["task_id"], "event": kind, "channel": "feishu",
            "target": data["feishu_target"], "message": message, "state": "pending", "attempts": 0,
            "created_at": now(), "max_attempts": 3, "next_attempt_at": 0}
    return Path(root or ROOT) / "data/outbox" / (key + ".json")


def ensure_terminal_intent(data, root=None):
    if data.get("status") not in TERMINAL or not data.get("feishu_target"):
        return False
    kind = "terminal-" + data["status"]
    key = notification_key(data["task_id"], kind, data["feishu_target"])
    changed = key not in data.get("notification_events", {})
    detail = str(data.get("failure_reason") or data.get("output") or data.get("last_health_event")
                 or "Terminal state recorded; no additional output available")
    receipt = remember_notification(data, kind,
        f"Codex 任务{'完成' if data['status'] == 'done' else '失败'}：{data.get('task_name', data['task_id'])}\n{detail[-1000:]}", root)
    changed = changed or data.get("notification_receipt") != str(receipt)
    data["notification_receipt"] = str(receipt)
    return changed


def reconcile_derived(session_dir, data, root=None):
    """Rebuild mirrors/outbox from committed intents, never reset delivery budgets."""
    mirror(session_dir, data, root)
    for key, item in data.get("notification_events", {}).items():
        if key != notification_key(item["task_id"], item["event"], item["target"]):
            raise ValueError("notification_identity_mismatch")
        path = Path(root or ROOT) / "data/outbox" / (key + ".json")
        if not path.exists():
            atomic_json(path, item)


def reconcile(session_dir, task_id, *, root=None):
    with lock(session_dir):
        data = current(session_dir, task_id)
        # Also closes legacy terminal records committed before an outbox existed.
        if ensure_terminal_intent(data, root):
            return commit(session_dir, data, root)
        reconcile_derived(session_dir, data, root)
        return data


def pending_receipts(data, *, root=None):
    result = []
    for key in data.get("notification_events", {}):
        path = Path(root or ROOT) / "data/outbox" / (key + ".json")
        item = read(path)
        if item["state"] != "delivered" and item["attempts"] < item["max_attempts"]:
            result.append(path)
    return result


def queue_notification(session_dir, task_id, kind, message, *, root=None):
    with lock(session_dir):
        data = current(session_dir, task_id)
        receipt = remember_notification(data, kind, message, root)
        if receipt:
            commit(session_dir, data, root)
        return receipt


def commit(session_dir, data, root=None):
    ensure_terminal_intent(data, root)
    data["revision"] = int(data.get("revision", 0)) + 1
    data["timestamp"] = now()
    atomic_json(Path(session_dir) / "task-meta.json", data)
    reconcile_derived(session_dir, data, root)
    return data


def update(session_dir, task_id, updates, *, root=None):
    with lock(session_dir):
        data = current(session_dir, task_id)
        # Re-read inside the transaction; never copy a caller's stale snapshot.
        if data.get("status") in TERMINAL:
            return reconcile(session_dir, task_id, root=root)
        for key in ("task_id", "revision", "started_at", "tmux_instance", "tmux_generation"):
            if key in updates and key in data and updates[key] != data[key]:
                raise StaleTask("immutable_task_identity")
        data.update(updates)
        if data.get("status") not in TERMINAL:
            data.pop("completed_at", None)
            data.pop("exit_code", None)
        return commit(session_dir, data, root)


def initialize(session_dir, data):
    with lock(session_dir):
        identity(data["task_id"])
        meta_path = Path(session_dir) / "task-meta.json"
        if meta_path.exists():
            prior = read(meta_path)
            if prior.get("status") not in TERMINAL:
                raise ValueError("active_task_exists_use_original_session")
        socket, name = data.get("tmux_socket"), data.get("tmux_session")
        if socket and name and subprocess.run(["tmux", "-S", socket, "has-session", "-t", "=" + name],
                                              capture_output=True).returncode == 0:
            raise ValueError("tmux_session_exists_no_implicit_replacement")
        return commit(session_dir, data)


def enqueue(meta, kind, message, *, root=None):
    """Call under task lock. Success means queued, never delivered."""
    if not meta.get("feishu_target"):
        return None
    root = Path(root or ROOT)
    key = notification_key(meta["task_id"], kind, meta["feishu_target"])
    path = root / "data/outbox" / (key + ".json")
    if not path.exists():
        atomic_json(path, {"task_id": meta["task_id"], "event": kind, "channel": "feishu",
            "target": meta["feishu_target"], "message": message, "state": "pending", "attempts": 0,
            "created_at": now(), "max_attempts": 3, "next_attempt_at": 0})
    return path


def schedule_delivery(path):
    if path:
        subprocess.Popen([sys.executable, str(ROOT / "task_state.py"), "drain", "--receipt", str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)


def drain(path):
    path = Path(path).resolve()
    if path.parent != (ROOT / "data/outbox").resolve():
        raise ValueError("invalid_outbox_path")
    with path.with_suffix(".lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            item = read(path)
            if item["state"] == "delivered":
                return
            if item["attempts"] >= item["max_attempts"]:
                if item["state"] == "sending":
                    item.update(state="failed", last_error="interrupted_delivery_unconfirmed")
                    atomic_json(path, item)
                return
            time.sleep(max(0, min(30, item.get("next_attempt_at", 0) - time.time())))
            item["attempts"] += 1
            item.update(state="sending", attempt_started_at=now())
            atomic_json(path, item)
            try:
                proc = subprocess.run(["timeout", "8", "/usr/bin/openclaw", "message", "send", "--channel",
                    item["channel"], "--target", item["target"], "--message", item["message"]],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                code = proc.returncode
            except (OSError, subprocess.TimeoutExpired):
                code = 124
            item["last_exit_code"] = code
            if code == 0:
                item.update(state="delivered", delivered_at=now())
            else:
                item.update(state="failed" if item["attempts"] >= item["max_attempts"] else "pending",
                            next_attempt_at=time.time() + 5 * item["attempts"])
            atomic_json(path, item)


def hook(session_dir, task_id, status, output, exit_code, thread_id="", turn_id=""):
    with lock(session_dir):
        meta = current(session_dir, task_id)
        if meta.get("thread_id") and thread_id and meta["thread_id"] != thread_id:
            raise StaleTask("stale_hook_thread")
        seen = meta.get("hook_turn_ids", [])
        if turn_id and turn_id in seen:
            meta = reconcile(session_dir, task_id)
            return meta, meta.get("notification_receipt")
        if meta.get("status") in TERMINAL:
            # Permit enriching a terminal worker failure with its first Hook,
            # never reopening or changing an already committed terminal result.
            if meta["status"] != status or meta.get("terminal_hook_recorded"):
                meta = reconcile(session_dir, task_id)
                return meta, meta.get("notification_receipt")
        meta.update(status=status, output=output, last_turn_completed_at=now(),
                    last_turn_thread_id=thread_id or meta.get("thread_id", ""))
        if thread_id:
            meta["thread_id"] = thread_id
        if turn_id:
            meta["hook_turn_ids"] = (seen + [turn_id])[-256:]
        if status in TERMINAL:
            failed_stage = "startup_failed" if meta.get("lifecycle_stage") == "startup_failed" else "worker_failed"
            meta.update(completed_at=meta.get("completed_at") or now(), exit_code=exit_code,
                        lifecycle_stage="completed" if status == "done" else failed_stage, terminal_hook_recorded=True)
        else:
            meta.pop("completed_at", None)
            meta.pop("exit_code", None)
            if status == "waiting_input":
                meta["lifecycle_stage"] = "waiting_input"
            elif meta.get("lifecycle_stage") not in {"dispatch_started", "worker_starting", "worker_started", "business_started"}:
                meta["lifecycle_stage"] = "business_started"
        meta = commit(session_dir, meta)
        return meta, meta.get("notification_receipt") if status in TERMINAL else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "update", "hook", "drain", "notify"])
    parser.add_argument("--session-dir")
    parser.add_argument("--task-id")
    parser.add_argument("--status")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--turn-id", default="")
    parser.add_argument("--receipt")
    parser.add_argument("--kind", default="startup")
    args = parser.parse_args()
    try:
        if args.action == "drain":
            drain(args.receipt)
            return 0
        if args.action == "init":
            initialize(args.session_dir, json.load(sys.stdin))
        elif args.action == "update":
            update(args.session_dir, args.task_id, json.load(sys.stdin))
        elif args.action == "hook":
            _, receipt = hook(args.session_dir, args.task_id, args.status, sys.stdin.read(), args.exit_code,
                              args.thread_id, args.turn_id)
            schedule_delivery(receipt)
        elif args.action == "notify":
            receipt = queue_notification(args.session_dir, args.task_id, args.kind, sys.stdin.read())
            schedule_delivery(receipt)
        return 0
    except StaleTask as exc:
        print(json.dumps({"ok": False, "ignored": str(exc)}))
        return 0  # A stale callback is ignored, not a new task failure.
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
