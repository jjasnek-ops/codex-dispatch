#!/bin/bash
# Codex turn adapter. Generation fencing, state transactions and outbox are shared.
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TASK_ID="${CODING_AGENT_TASK_ID:-}"
SESSION_DIR="${CODING_AGENT_SESSION_DIR:-}"
if [ -z "$TASK_ID" ] || [ -z "$SESSION_DIR" ]; then exit 0; fi
EXIT_CODE="${CODEX_EXIT_CODE:-0}"
[[ "$EXIT_CODE" =~ ^[0-9]+$ ]] || EXIT_CODE=1
THREAD_ID=""
TURN_ID=""
INTERACTIVE_EVENT=0
PAYLOAD="${1:-}"
OUTPUT=""
if [ -n "$PAYLOAD" ]; then
    EVENT_TYPE=$(printf '%s' "$PAYLOAD" | jq -r '.type // empty')
    [ "$EVENT_TYPE" = "agent-turn-complete" ] || exit 0
    INTERACTIVE_EVENT=1
    THREAD_ID=$(printf '%s' "$PAYLOAD" | jq -r '."thread-id" // empty')
    TURN_ID=$(printf '%s' "$PAYLOAD" | jq -r '."turn-id" // empty')
    OUTPUT=$(printf '%s' "$PAYLOAD" | jq -r '."last-assistant-message" // empty')
else
    for candidate in "${CODEX_LAST_MSG_FILE:-}" "${CODEX_TASK_OUTPUT:-$SESSION_DIR/task-output.txt}"; do
        if [ -n "$candidate" ] && [ -s "$candidate" ]; then
            OUTPUT=$(cat "$candidate")
            break
        fi
    done
    [ -n "$OUTPUT" ] || OUTPUT="任务结束（exit_code=${EXIT_CODE}），但输出为空。"
fi
check_task_completion() {
    if [ "$EXIT_CODE" -ne 0 ]; then echo failed; return; fi
    local structured
    if structured=$(printf '%s' "$OUTPUT" | jq -ers --arg interactive "$INTERACTIVE_EVENT" '
        if length != 1 or (.[0] | type) != "object" then error("not one object") else .[0] |
          if .ok == false or .status == "failed" or .status == "error" or
             ((.exit_code | type) == "number" and .exit_code != 0) then "failed"
          elif .status == "waiting_input" or .status == "blocked" then "waiting_input"
          elif .ok == true and (.status == "success" or .status == "success_with_warnings") then "done"
          elif $interactive == "1" then "running" else "waiting_input" end end' 2>/dev/null); then
        echo "$structured"
    elif printf '%s' "$OUTPUT" | grep -Eqi '(Task failed|任务失败|status["[:space:]]*[=:][[:space:]]*(failed|error)|exit[_ -]?code["[:space:]]*[=:][[:space:]]*[1-9]|退出码[：:][[:space:]]*[1-9])'; then
        echo failed
    elif printf '%s' "$OUTPUT" | grep -qE '(Interrupted.*What should Codex do|请指示如何继续|用户是否希望我：|AskUserQuestion)'; then
        echo waiting_input
    elif [ "$INTERACTIVE_EVENT" = 1 ]; then echo running
    else echo done
    fi
}
STATUS=$(check_task_completion)
if [ "$STATUS" = failed ] && [ -n "${CODEX_FAILURE_REASON:-}" ]; then
    OUTPUT="Codex dispatch health check failed: ${CODEX_FAILURE_REASON}
${OUTPUT}"
fi
printf '%s' "$OUTPUT" | python3 "$SKILL_DIR/task_state.py" hook \
    --session-dir "$SESSION_DIR" --task-id "$TASK_ID" --status "$STATUS" \
    --exit-code "$EXIT_CODE" --thread-id "$THREAD_ID" --turn-id "$TURN_ID"
