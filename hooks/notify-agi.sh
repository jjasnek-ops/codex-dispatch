#!/bin/bash
# notify-agi.sh — codex-agent 任务完成通知
#
# 调用方式：
#   交互式模式（codex notify）：notify-agi.sh '<json>'
#   headless 模式（dispatch-codex.sh）：环境变量方式调用

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESULT_DIR="${SKILL_DIR}/data"
LOG="${RESULT_DIR}/hook.log"
OPENCLAW_BIN="/usr/bin/openclaw"

mkdir -p "$RESULT_DIR" "$RESULT_DIR/tasks"

log() { echo "[$(date -Iseconds)] $*" >> "$LOG"; }

check_task_completion() {
    local output="$1"
    local exit_code="${2:-0}"

    if [[ ! "$exit_code" =~ ^[0-9]+$ ]]; then
        exit_code=1
    fi

    if [ "$exit_code" -ne 0 ]; then
        echo "failed"
        return
    fi

    if echo "$output" | grep -qE "(Interrupted.*What should Codex do|请指示如何继续|用户是否希望我：|AskUserQuestion)"; then
        echo "waiting_input"
        return
    fi

    if echo "$output" | grep -qE "(任务.*完成|Task completed|Successfully completed|完成标记|✅)"; then
        echo "done"
        return
    fi

    if echo "$output" | grep -qE "(git commit|Co-Authored-By:)"; then
        echo "done"
        return
    fi

    echo "done"
}

log "=== notify-agi.sh fired ==="

PAYLOAD="${1:-}"
THREAD_ID=""
OUTPUT=""
TASK_ID="${CODING_AGENT_TASK_ID:-}"
SESSION_DIR="${CODING_AGENT_SESSION_DIR:-$RESULT_DIR}"
EXIT_CODE="${CODEX_EXIT_CODE:-0}"
if [[ ! "$EXIT_CODE" =~ ^[0-9]+$ ]]; then
    EXIT_CODE=1
fi

if [ -n "$PAYLOAD" ]; then
    event_type="$(printf '%s' "$PAYLOAD" | jq -r '.type // empty' 2>/dev/null || true)"
    if [ "$event_type" != "agent-turn-complete" ]; then
        log "Ignoring event type: $event_type"
        exit 0
    fi

    THREAD_ID="$(printf '%s' "$PAYLOAD" | jq -r '."thread-id" // empty' 2>/dev/null || true)"
    TURN_ID="$(printf '%s' "$PAYLOAD" | jq -r '."turn-id" // empty' 2>/dev/null || true)"
    CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null || true)"
    OUTPUT="$(printf '%s' "$PAYLOAD" | jq -r '."last-assistant-message" // empty' 2>/dev/null || true)"

    log "interactive mode: thread=$THREAD_ID turn=$TURN_ID cwd=$CWD"
else
    LAST_MSG_FILE="${CODEX_LAST_MSG_FILE:-}"
    JSONL_FILE="${CODEX_JSONL_FILE:-}"
    TASK_OUTPUT_FILE="${CODEX_TASK_OUTPUT:-${RESULT_DIR}/task-output.txt}"

    log "headless mode: task_id=$TASK_ID exit_code=$EXIT_CODE"

    if [ -z "$TASK_ID" ]; then
        log "ERROR: CODING_AGENT_TASK_ID not set"
        exit 1
    fi

    if [ -n "$LAST_MSG_FILE" ] && [ -f "$LAST_MSG_FILE" ] && [ -s "$LAST_MSG_FILE" ]; then
        OUTPUT=$(cat "$LAST_MSG_FILE" | head -c 3000)
        log "Output from last-message file (${#OUTPUT} chars)"
    fi

    if [ -z "$OUTPUT" ] && [ -n "$JSONL_FILE" ] && [ -f "$JSONL_FILE" ] && [ -s "$JSONL_FILE" ]; then
        OUTPUT=$(grep -o '"content":"[^"]*"' "$JSONL_FILE" 2>/dev/null | tail -5 | sed 's/"content":"//;s/"$//' | tr '\n' ' ' | head -c 2000 || true)
        [ -n "$OUTPUT" ] && log "Output from JSONL file (${#OUTPUT} chars)"
    fi

    if [ -z "$OUTPUT" ] && [ -f "$TASK_OUTPUT_FILE" ] && [ -s "$TASK_OUTPUT_FILE" ]; then
        OUTPUT=$(tail -c 3000 "$TASK_OUTPUT_FILE")
        log "Output from task-output.txt (${#OUTPUT} chars)"
    fi

    [ -z "$OUTPUT" ] && OUTPUT="任务结束（exit_code=${EXIT_CODE}），但输出为空。"
fi

META_FILE=""
if [ -n "$SESSION_DIR" ] && [ -f "${SESSION_DIR}/task-meta.json" ]; then
    META_FILE="${SESSION_DIR}/task-meta.json"
elif [ -f "${RESULT_DIR}/task-meta.json" ]; then
    META_FILE="${RESULT_DIR}/task-meta.json"
fi

TASK_NAME="unknown"
FEISHU_TARGET=""
TASK_COMPLETED_AT=""

if [ -n "$META_FILE" ]; then
    TASK_NAME=$(jq -r '.task_name // "unknown"' "$META_FILE" 2>/dev/null || echo "unknown")
    FEISHU_TARGET=$(jq -r '.feishu_target // ""' "$META_FILE" 2>/dev/null || echo "")
    TASK_COMPLETED_AT=$(jq -r '.completed_at // ""' "$META_FILE" 2>/dev/null || echo "")
    [ -z "$TASK_ID" ] && TASK_ID=$(jq -r '.task_id // ""' "$META_FILE" 2>/dev/null || echo "")
fi

[ -z "$TASK_ID" ] && TASK_ID="adhoc-$(date +%s)"
log "meta: task=$TASK_NAME target=$FEISHU_TARGET task_id=$TASK_ID"

DONE_TS="$(date -Iseconds)"
COMPLETION_STATUS=$(check_task_completion "$OUTPUT" "$EXIT_CODE")
log "Task completion status: $COMPLETION_STATUS"

WRITE_STATUS="$COMPLETION_STATUS"

TASK_FILE="${RESULT_DIR}/tasks/${TASK_ID}.json"
if [ -f "$TASK_FILE" ]; then
    jq --arg ts "$DONE_TS" \
       --arg output "$OUTPUT" \
       --arg thread_id "${THREAD_ID:-}" \
       --arg status "$WRITE_STATUS" \
       --arg exit_code "$EXIT_CODE" \
       '. + {status: $status, timestamp: $ts, output: $output, completed_at: $ts, thread_id: $thread_id, exit_code: ($exit_code | tonumber)}' \
       "$TASK_FILE" > "${TASK_FILE}.tmp" 2>/dev/null && mv "${TASK_FILE}.tmp" "$TASK_FILE"
else
    jq -n \
        --arg ts "$DONE_TS" \
        --arg output "$OUTPUT" \
        --arg task "$TASK_NAME" \
        --arg task_id "$TASK_ID" \
        --arg target "$FEISHU_TARGET" \
        --arg thread_id "${THREAD_ID:-}" \
        --arg status "$WRITE_STATUS" \
        --arg exit_code "$EXIT_CODE" \
        '{task_name: $task, task_id: $task_id, feishu_target: $target, timestamp: $ts, output: $output, status: $status, completed_at: $ts, thread_id: $thread_id, exit_code: ($exit_code | tonumber)}' \
        > "$TASK_FILE" 2>/dev/null
fi
log "Wrote tasks/${TASK_ID}.json (status=$WRITE_STATUS)"

jq -n \
    --arg ts "$DONE_TS" \
    --arg output "$OUTPUT" \
    --arg task "$TASK_NAME" \
    --arg task_id "$TASK_ID" \
    --arg target "$FEISHU_TARGET" \
    --arg status "$WRITE_STATUS" \
    --arg exit_code "$EXIT_CODE" \
    '{task_name: $task, task_id: $task_id, feishu_target: $target, timestamp: $ts, output: $output, status: $status, exit_code: ($exit_code | tonumber)}' \
    > "${RESULT_DIR}/latest.json" 2>/dev/null
log "Wrote latest.json"

if [ -n "$META_FILE" ] && [ -f "$META_FILE" ]; then
    jq --arg ts "$DONE_TS" --arg status "$WRITE_STATUS" --arg exit_code "$EXIT_CODE" \
       '. + {completed_at: $ts, status: $status, exit_code: ($exit_code | tonumber)}' \
        "$META_FILE" > "${META_FILE}.tmp" 2>/dev/null && mv "${META_FILE}.tmp" "$META_FILE"
fi

if [ -z "$TASK_COMPLETED_AT" ]; then
    TASK_COMPLETED_AT="$DONE_TS"
fi

if [ "$COMPLETION_STATUS" = "waiting_input" ]; then
    log "Task not completed (status=$COMPLETION_STATUS), skip notifications"
    log "=== notify-agi.sh completed ==="
    exit 0
fi

LOCK_FILE="${RESULT_DIR}/.hook-lock"
LOCK_AGE_LIMIT=30
SKIP_NOTIFY=false
HAS_VALID_OUTPUT=false
if [ -n "$OUTPUT" ] && ! echo "$OUTPUT" | grep -qF "输出为空"; then
    HAS_VALID_OUTPUT=true
fi
if [ "$COMPLETION_STATUS" = "failed" ]; then
    # A failed task must still produce a real failure notification even when
    # the runner emitted no final assistant message.
    HAS_VALID_OUTPUT=true
fi

if [ "$HAS_VALID_OUTPUT" = true ]; then
    if [ -f "$LOCK_FILE" ]; then
        LOCK_TIME=$(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0)
        NOW=$(date +%s)
        AGE=$(( NOW - LOCK_TIME ))
        if [ "$AGE" -lt "$LOCK_AGE_LIMIT" ]; then
            SKIP_NOTIFY=true
            log "Duplicate hook within ${AGE}s, skip notifications"
        fi
    fi
    if [ "$SKIP_NOTIFY" != true ]; then
        touch "$LOCK_FILE"
    fi
fi

NOTIFY_KEY="${TASK_NAME}|${TASK_COMPLETED_AT}|${FEISHU_TARGET}"
NOTIFY_KEY_FILE="${RESULT_DIR}/.last-notify-key"
if [ -f "$NOTIFY_KEY_FILE" ] && [ "$(cat "$NOTIFY_KEY_FILE" 2>/dev/null)" = "$NOTIFY_KEY" ]; then
    SKIP_NOTIFY=true
    log "Skip duplicate notification for key=$NOTIFY_KEY"
fi

if [ "$SKIP_NOTIFY" = true ]; then
    log "=== notify-agi.sh completed ==="
    exit 0
fi

if [ -n "$FEISHU_TARGET" ] && [ -x "$OPENCLAW_BIN" ]; then
    SUMMARY=$(echo "$OUTPUT" | tail -c 1000 | tr '\n' ' ')
    if [ "$COMPLETION_STATUS" = "failed" ]; then
        STATUS_LABEL="失败"
    else
        STATUS_LABEL="完成"
    fi
    MSG="🤖 *Codex 任务${STATUS_LABEL}*
📋 任务: ${TASK_NAME}
🔢 退出码: ${EXIT_CODE}
📝 结果摘要:
\`\`\`
${SUMMARY:0:800}
\`\`\`"

    timeout 8 "$OPENCLAW_BIN" message send \
        --channel feishu \
        --target "$FEISHU_TARGET" \
        --message "$MSG" 2>/dev/null && log "Sent Feishu message to $FEISHU_TARGET" || log "Feishu send failed"
fi

echo "$NOTIFY_KEY" > "$NOTIFY_KEY_FILE" 2>/dev/null || true
log "Recorded notify key: $NOTIFY_KEY"

log "=== notify-agi.sh completed ==="
exit 0
