#!/usr/bin/env python3
"""Run Codex CLI reliably (headless or interactive via tmux).

Headless mode: codex exec "prompt" --json -o FILE -C workdir
Tmux mode: run codex-wrapper.sh inside a tmux session
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import shutil
import tempfile
import time
from pathlib import Path

DEFAULT_CODEX = os.environ.get("CODEX_BIN") or shutil.which("codex") or "/usr/local/bin/codex"
SCRIPT_DIR = Path(__file__).parent
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_CONFIG_FILE = Path.home() / ".codex" / "config.toml"
FALLBACK_CODEX_BASE_URL = os.environ.get("CODEX_FALLBACK_BASE_URL", "")
CODEX_BASE_URL = os.environ.get("OPENAI_BASE_URL") or FALLBACK_CODEX_BASE_URL


def _read_codex_auth_mode() -> str:
    try:
        data = json.loads(CODEX_AUTH_FILE.read_text())
        return str(data.get("auth_mode") or "").strip().lower()
    except Exception:
        return ""


CODEX_AUTH_MODE = _read_codex_auth_mode()


def codex_model_args(args: argparse.Namespace) -> list[str]:
    model = (args.model or "").strip()
    return ["-m", model] if model else []


def _ensure_directory_trusted(cwd: str | None) -> None:
    """Auto-add cwd (and its git root) to ~/.codex/config.toml as trusted."""
    if not cwd:
        return
    paths_to_trust: list[str] = [os.path.abspath(cwd)]
    # Also trust git repo root if inside a git repo
    try:
        git_root = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        if git_root and git_root not in paths_to_trust:
            paths_to_trust.append(git_root)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if not CODEX_CONFIG_FILE.exists():
        return

    content = CODEX_CONFIG_FILE.read_text()
    added = False
    for p in paths_to_trust:
        section_key = f'[projects."{p}"]'
        if section_key not in content:
            content = content.rstrip("\n") + f"\n\n{section_key}\ntrust_level = \"trusted\"\n"
            added = True
    if added:
        CODEX_CONFIG_FILE.write_text(content)
        print(f"Auto-trusted directories in config.toml: {paths_to_trust}", flush=True)


NOTIFY_SCRIPT = str(SCRIPT_DIR / "hooks" / "notify-agi.sh")


def _ensure_notify_configured() -> None:
    """Ensure notify hook is configured in ~/.codex/config.toml.

    The notify line can be accidentally lost when config.toml is edited
    (e.g., model migration, manual edits). This function restores it.
    """
    if not CODEX_CONFIG_FILE.exists():
        return

    content = CODEX_CONFIG_FILE.read_text()
    notify_line = f'notify = ["{NOTIFY_SCRIPT}"]'

    if notify_line in content:
        return

    if re.search(r"^notify\s*=", content, re.MULTILINE):
        return  # Has some notify config, don't override

    lines = content.split("\n")
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[notice"):
            insert_idx = i
            break

    lines.insert(insert_idx, notify_line)
    CODEX_CONFIG_FILE.write_text("\n".join(lines))
    print(f"Restored notify config in config.toml: {NOTIFY_SCRIPT}", flush=True)


def _read_codex_api_key() -> str:
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key

    try:
        data = json.loads(CODEX_AUTH_FILE.read_text())
    except Exception:
        data = {}

    auth_key = str(data.get("OPENAI_API_KEY") or "").strip()
    if auth_key:
        return auth_key

    return ""


def which(name: str) -> str | None:
    paths = os.environ.get("PATH", "").split(":")
    for p in paths:
        cand = Path(p) / name
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:
            pass
    return None


def build_headless_cmd(args: argparse.Namespace) -> list[str]:
    cmd: list[str] = [
        args.codex_bin,
        "--no-daemon",
        "exec",
        *codex_model_args(args),
    ]

    if args.resume:
        cmd += ["resume", args.resume]

    if args.prompt is not None:
        cmd.append(args.prompt)

    if args.json_output:
        cmd.append("--json")

    if args.output_file:
        cmd += ["-o", args.output_file]

    if args.cwd:
        cmd += ["-C", args.cwd]

    if args.sandbox:
        cmd += ["-s", args.sandbox]

    if args.skip_git_repo_check:
        cmd.append("--skip-git-repo-check")

    if args.yolo:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")

    if args.extra:
        cmd += args.extra

    return cmd


def extract_thread_id_from_jsonl(output: str) -> str | None:
    """Extract thread_id from JSONL output."""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            tid = event.get("thread_id") or event.get("session_id") or event.get("id")
            if tid:
                return str(tid)
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def run_headless(args: argparse.Namespace) -> int:
    _ensure_directory_trusted(args.cwd)
    _ensure_notify_configured()
    cmd = build_headless_cmd(args)
    print(f"Running: {' '.join(shlex.quote(c) for c in cmd)}", flush=True)

    proc = subprocess.run(
        cmd,
        cwd=args.cwd,
        capture_output=False,
        text=True,
    )

    # Try to extract thread_id from output file if available
    if args.output_file and Path(args.output_file).exists():
        try:
            content = Path(args.output_file).read_text()
            thread_id = extract_thread_id_from_jsonl(content)
            if thread_id:
                _save_thread_id(thread_id, args)
        except Exception:
            pass

    return proc.returncode


def _save_thread_id(thread_id: str, args: argparse.Namespace) -> None:
    """Save thread_id to task-meta.json if SESSION_DIR is set."""
    session_dir = os.environ.get("CODING_AGENT_SESSION_DIR", "")
    if not session_dir:
        return
    meta_file = Path(session_dir) / "task-meta.json"
    if not meta_file.exists():
        return
    try:
        meta = json.loads(meta_file.read_text())
        meta["thread_id"] = thread_id
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"Saved thread_id={thread_id} to task-meta.json", flush=True)
    except Exception as e:
        print(f"Warning: failed to save thread_id: {e}", file=sys.stderr)


# ---- tmux helpers ----

def tmux_cmd(socket_path: str, *args: str) -> list[str]:
    return ["tmux", "-S", socket_path, *args]


def tmux_capture(socket_path: str, target: str, lines: int = 200) -> str:
    out = subprocess.check_output(
        tmux_cmd(socket_path, "capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}"),
        text=True,
    )
    return out


def tmux_wait_for_text(socket_path: str, target: str, pattern: str, timeout_s: int = 30, poll_s: float = 0.5) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            buf = tmux_capture(socket_path, target, lines=200)
            if pattern in buf:
                return True
        except subprocess.CalledProcessError:
            pass
        time.sleep(poll_s)
    return False


def tmux_wait_for_any_text(
    socket_path: str,
    target: str,
    patterns: list[str],
    timeout_s: int = 30,
    poll_s: float = 0.5,
) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            buf = tmux_capture(socket_path, target, lines=240)
            for pattern in patterns:
                if pattern in buf:
                    return True
        except subprocess.CalledProcessError:
            pass
        time.sleep(poll_s)
    return False


def compact_tmux_prompt(prompt: str | None) -> str | None:
    if prompt is None:
        return None

    compact_lines: list[str] = []
    for line in prompt.splitlines():
        cleaned = line.rstrip()
        if not cleaned.strip():
            continue
        compact_lines.append(cleaned)

    return "\n".join(compact_lines).strip()


def tmux_paste_prompt(socket_path: str, target: str, prompt: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding="utf-8") as f:
        f.write(prompt)
        temp_file = f.name

    try:
        subprocess.check_call(tmux_cmd(socket_path, "load-buffer", temp_file))
        subprocess.check_call(tmux_cmd(socket_path, "paste-buffer", "-d", "-t", target))
        paste_wait_s = min(1.5, 0.35 + (len(prompt) / 6000.0))
        time.sleep(paste_wait_s)
        subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))
        time.sleep(1.0)
        try:
            pane_after_enter = tmux_capture(socket_path, target, lines=140)
            if "[Pasted Content" in pane_after_enter or "[Pasted text #" in pane_after_enter:
                subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))
                time.sleep(1.0)
        except subprocess.CalledProcessError:
            pass
    finally:
        Path(temp_file).unlink(missing_ok=True)


def run_tmux(args: argparse.Namespace) -> int:
    _ensure_directory_trusted(args.cwd)
    _ensure_notify_configured()
    if not which("tmux"):
        print("tmux not found in PATH; cannot run tmux mode.", file=sys.stderr)
        return 2

    socket_dir = args.tmux_socket_dir or os.environ.get("CLAWDBOT_TMUX_SOCKET_DIR") or "/root/clawdbot-tmux-sockets"
    Path(socket_dir).mkdir(parents=True, exist_ok=True)
    socket_path = str(Path(socket_dir) / args.tmux_socket_name)

    session = args.tmux_session
    target = f"{session}:0.0"

    cwd = args.cwd or os.getcwd()
    prompt = compact_tmux_prompt(args.prompt)
    if args.prompt and prompt and prompt != args.prompt:
        print(f"Compacted tmux prompt: {len(args.prompt)} -> {len(prompt)} chars", flush=True)

    # Create new session, replacing old one if it exists
    # Note: kill-session on the last session kills the server too, so we
    # create the new session first, then kill the old one.
    session_exists = subprocess.run(
        tmux_cmd(socket_path, "has-session", "-t", session),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0

    if session_exists:
        # Create new session with temp name, kill old, rename
        tmp_session = f"{session}__new"
        subprocess.check_call(tmux_cmd(socket_path, "new", "-d", "-s", tmp_session, "-n", "shell"))
        subprocess.run(tmux_cmd(socket_path, "kill-session", "-t", session), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(tmux_cmd(socket_path, "rename-session", "-t", tmp_session, session))
    else:
        subprocess.check_call(tmux_cmd(socket_path, "new", "-d", "-s", session, "-n", "shell"))

    # Start session self-destruct timer (independent process group)
    session_ttl = int(os.environ.get("SESSION_TTL", "43200"))  # default 12 hours
    subprocess.Popen(
        ["setsid", "bash", "-c",
         f"sleep {session_ttl} && tmux -S {shlex.quote(socket_path)} kill-session -t {shlex.quote(session)} 2>/dev/null"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # Inject API config at tmux session level first, then shell as backup.
    # This matches coding-agent and reduces timing issues for spawned shell processes.
    openai_api_key = _read_codex_api_key()
    if CODEX_BASE_URL:
        subprocess.check_call(tmux_cmd(socket_path, "set-environment", "-t", session, "OPENAI_BASE_URL", CODEX_BASE_URL))
        print("OPENAI_BASE_URL injected into tmux session", flush=True)
    else:
        subprocess.run(tmux_cmd(socket_path, "set-environment", "-u", "-t", session, "OPENAI_BASE_URL"),
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if openai_api_key:
        subprocess.check_call(tmux_cmd(socket_path, "set-environment", "-t", session, "OPENAI_API_KEY", openai_api_key))
        print("OPENAI_API_KEY injected into tmux session", flush=True)
    else:
        subprocess.run(tmux_cmd(socket_path, "set-environment", "-u", "-t", session, "OPENAI_API_KEY"),
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Inject CODING_AGENT_* env vars + API config
    # Note: codex interactive mode ignores model_provider in config.toml,
    # must inject OPENAI_BASE_URL and OPENAI_API_KEY explicitly
    coding_agent_vars = {
        "CODING_AGENT_TASK_ID": os.environ.get("CODING_AGENT_TASK_ID", ""),
        "CODING_AGENT_SESSION_DIR": os.environ.get("CODING_AGENT_SESSION_DIR", ""),
        "CODING_AGENT_TMUX_SESSION": os.environ.get("CODING_AGENT_TMUX_SESSION", ""),
        "CODING_AGENT_WORKDIR": os.environ.get("CODING_AGENT_WORKDIR", ""),
        "FEISHU_TARGET": os.environ.get("FEISHU_TARGET", ""),
    }
    for var_name, var_val in coding_agent_vars.items():
        if var_val:
            subprocess.check_call(tmux_cmd(socket_path, "set-environment", "-t", session, var_name, var_val))
            subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "-l", "--", f"export {var_name}={shlex.quote(var_val)}"))
            subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))
            time.sleep(0.05)

    if CODEX_BASE_URL:
        subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "-l", "--", f"export OPENAI_BASE_URL={shlex.quote(CODEX_BASE_URL)}"))
        subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))
        time.sleep(0.05)

    if openai_api_key:
        subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "-l", "--", f"export OPENAI_API_KEY={shlex.quote(openai_api_key)}"))
        subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))
        time.sleep(0.05)

    # Build interactive codex command (no 'exec' subcommand = interactive TUI mode)
    # Note: -a never and --dangerously-bypass-approvals-and-sandbox are mutually exclusive
    cmd_parts = [
        args.codex_bin,
        "--no-daemon",
        *codex_model_args(args),
        "-C",
        cwd,
    ]
    if args.yolo:
        cmd_parts += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        cmd_parts += ["-a", "never"]
        if args.sandbox:
            cmd_parts += ["-s", args.sandbox]

    launch = f"cd {shlex.quote(cwd)} && " + " ".join(shlex.quote(p) for p in cmd_parts)
    subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "-l", "--", launch))
    subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))

    if tmux_wait_for_any_text(
        socket_path,
        target,
        patterns=["Try new model", "Use existing model"],
        timeout_s=5,
        poll_s=0.3,
    ):
        subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Down"))
        time.sleep(0.2)
        subprocess.check_call(tmux_cmd(socket_path, "send-keys", "-t", target, "Enter"))
        time.sleep(1.0)

    if prompt:
        tmux_wait_for_any_text(
            socket_path,
            target,
            patterns=["OpenAI Codex", "Tip:", "›", ">"],
            timeout_s=20,
            poll_s=0.4,
        )
        time.sleep(0.2)
        tmux_paste_prompt(socket_path, target, prompt)

    print(f"Started codex-agent in tmux session: {session}")
    print(f"To monitor: tmux -S {shlex.quote(socket_path)} attach -t {shlex.quote(session)}")

    if args.interactive_wait_s > 0:
        time.sleep(args.interactive_wait_s)
        try:
            snap = tmux_capture(socket_path, target, lines=200)
            print("\n--- tmux snapshot ---\n")
            print(snap)
        except subprocess.CalledProcessError:
            pass

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Codex CLI reliably (headless or tmux)")

    ap.add_argument("-p", "--prompt", help="Prompt text")
    ap.add_argument("--mode", choices=["headless", "tmux"], default="headless")
    ap.add_argument("--codex-bin", default=DEFAULT_CODEX)
    ap.add_argument("--model", default="", help="Model override; omit to use Codex default config")
    ap.add_argument("--cwd", help="Working directory")
    ap.add_argument("--sandbox", default=None, help="Sandbox mode (read-only/workspace-write/danger-full-access)")
    ap.add_argument("--skip-git-repo-check", action="store_true")
    ap.add_argument("--yolo", action="store_true", help="--dangerously-bypass-approvals-and-sandbox")
    ap.add_argument("--json-output", action="store_true", default=True, help="Pass --json to codex")
    ap.add_argument("--output-file", "-o", help="File for last message (-o)")
    ap.add_argument("--resume", help="Session ID to resume")

    ap.add_argument("--tmux-session", default="codex-agent")
    ap.add_argument("--tmux-socket-dir", default=None)
    ap.add_argument("--tmux-socket-name", default="codex-code.sock")
    ap.add_argument("--interactive-wait-s", type=int, default=0)

    ap.add_argument("extra", nargs=argparse.REMAINDER)

    args = ap.parse_args()

    extra = args.extra
    if extra and extra[0] == "--":
        extra = extra[1:]
    args.extra = extra

    if not Path(args.codex_bin).exists():
        print(f"codex binary not found: {args.codex_bin}", file=sys.stderr)
        return 2

    if args.mode == "tmux":
        return run_tmux(args)

    return run_headless(args)


if __name__ == "__main__":
    raise SystemExit(main())
