#!/bin/bash
# batch-dispatch-codex.sh — 批量串行派发 Codex 任务
#
# Usage:
#   batch-dispatch-codex.sh (--tasks tasks.json | --markdown-dir DIR) [OPTIONS]
#
# Task input:
#   --tasks FILE             JSON 任务文件；与 --markdown-dir 二选一
#   --markdown-dir DIR       Markdown 任务目录；与 --tasks 二选一
#   --markdown-pattern GLOB  Markdown 文件匹配规则（默认: part*.md）
#   --append-prompt TEXT     给每个任务追加同一段 Prompt
#   --append-prompt-file FILE
#                            覆盖公共要求文件；Markdown 模式默认读取同目录 common-requirements.md
#
# Other options:
#   -g, --group ID           飞书通知目标
#   -w, --workdir DIR        工作目录（默认: /root）
#   --sandbox MODE           沙箱模式（默认: 使用 config.toml）
#   --skip-git-repo-check    允许在非 git 仓库中运行
#   --model MODEL            模型覆盖；不传则使用 Codex 自身默认配置
#   --wait-timeout SECONDS   单个任务超时时间（默认: 3600）
#   --stop-on-error          任务失败时停止（默认: 继续）
#   --tmux-session NAME      tmux session 名（默认: codex-agent）
#   -h, --help               显示帮助信息
#
# JSON 格式:
#   {
#     "tasks": [
#       {"name": "task-1", "prompt": "任务 1 的 prompt"},
#       {"name": "task-2", "prompt": "任务 2 的 prompt"}
#     ]
#   }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPATCH_SCRIPT="$SCRIPT_DIR/dispatch-codex.sh"
RESULT_DIR="${SCRIPT_DIR}/data"
TASKS_DIR="${RESULT_DIR}/tasks"

# 默认值
TASKS_FILE=""
MARKDOWN_DIR=""
MARKDOWN_PATTERN="part*.md"
APPEND_PROMPT=""
APPEND_PROMPT_FILE=""
FEISHU_TARGET=""
CDP_PORT=""
WORKDIR="/root"
SANDBOX=""
SKIP_GIT_REPO_CHECK=""
MODEL=""
WAIT_TIMEOUT=3600
STOP_ON_ERROR=false
TMUX_SESSION="codex-agent"

# 统计变量
TOTAL_TASKS=0
SUCCESS_COUNT=0
FAILED_COUNT=0
TIMEOUT_COUNT=0
START_TIME=$(date +%s)
WAIT_RESULT=""

# 链式传递
LAST_PROMPT=""
LAST_TASK_ID=""

usage() {
    cat << EOF
批量串行派发 Codex 任务

用法:
  batch-dispatch-codex.sh (--tasks tasks.json | --markdown-dir DIR) [OPTIONS]

选项:
  --tasks FILE             JSON 任务文件；与 --markdown-dir 二选一
  --markdown-dir DIR       Markdown 目录；每个 .md 生成一个任务，按文件名自然排序
  --markdown-pattern GLOB  Markdown 文件名模式（默认: part*.md）
  --append-prompt TEXT     给每个任务末尾追加同一段 Prompt
  --append-prompt-file FILE
                           覆盖公共要求文件；Markdown 模式默认读取同目录 common-requirements.md
  -g, --group, --target ID 可选覆盖飞书通知目标；不传则由单任务 dispatch 自动选择
  --cdp PORT               浏览器 CDP 端口；自动传给每个 Part
  -w, --workdir DIR        工作目录（默认: /root）
  --sandbox MODE           沙箱模式
  --skip-git-repo-check    允许在非 git 仓库中运行
  --model MODEL            模型覆盖；不传则使用 Codex 自身默认配置
  --wait-timeout SECONDS   单个任务超时时间（默认: 3600）
  --stop-on-error          任务失败时停止（默认: 继续）
  --tmux-session NAME      tmux session 名（默认: codex-agent）
  -h, --help               显示帮助信息
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --tasks) TASKS_FILE="$2"; shift 2;;
            --markdown-dir) MARKDOWN_DIR="$2"; shift 2;;
            --markdown-pattern) MARKDOWN_PATTERN="$2"; shift 2;;
            --append-prompt) APPEND_PROMPT="$2"; shift 2;;
            --append-prompt-file) APPEND_PROMPT_FILE="$2"; shift 2;;
            -g|--group|--target) FEISHU_TARGET="$2"; shift 2;;
            --cdp) CDP_PORT="$2"; shift 2;;
            -w|--workdir) WORKDIR="$2"; shift 2;;
            --sandbox) SANDBOX="$2"; shift 2;;
            --skip-git-repo-check) SKIP_GIT_REPO_CHECK="1"; shift;;
            --model) MODEL="$2"; shift 2;;
            --wait-timeout) WAIT_TIMEOUT="$2"; shift 2;;
            --stop-on-error) STOP_ON_ERROR=true; shift;;
            --tmux-session) TMUX_SESSION="$2"; shift 2;;
            -h|--help) usage; exit 0;;
            *) echo "未知参数: $1" >&2; usage; exit 1;;
        esac
    done

    if [[ -n "$TASKS_FILE" && -n "$MARKDOWN_DIR" ]] || [[ -z "$TASKS_FILE" && -z "$MARKDOWN_DIR" ]]; then
        echo "错误: --tasks 与 --markdown-dir 必须且只能选择一个" >&2
        usage
        exit 1
    fi
    if [[ -n "$TASKS_FILE" && ! -f "$TASKS_FILE" ]]; then
        echo "错误: 任务文件不存在: $TASKS_FILE" >&2; exit 1
    fi
    if [[ -n "$MARKDOWN_DIR" && ! -d "$MARKDOWN_DIR" ]]; then
        echo "错误: Markdown 任务目录不存在: $MARKDOWN_DIR" >&2; exit 1
    fi
    if [[ -n "$MARKDOWN_DIR" && -z "$APPEND_PROMPT_FILE" && -f "$MARKDOWN_DIR/common-requirements.md" ]]; then
        APPEND_PROMPT_FILE="$MARKDOWN_DIR/common-requirements.md"
    fi
    if [[ -n "$APPEND_PROMPT_FILE" && ! -f "$APPEND_PROMPT_FILE" ]]; then
        echo "错误: 追加 Prompt 文件不存在: $APPEND_PROMPT_FILE" >&2; exit 1
    fi
}

load_tasks_json() {
    local tasks_json=""

    if [[ -n "$TASKS_FILE" ]]; then
        tasks_json=$(cat "$TASKS_FILE")
        echo "$tasks_json" | jq -e '.tasks | type == "array"' >/dev/null 2>&1 || {
            echo "错误: JSON 任务文件必须包含 tasks 数组: $TASKS_FILE" >&2
            return 1
        }
    else
        tasks_json='{"tasks":[]}'
        local markdown_file=""
        while IFS= read -r markdown_file; do
            local task_name
            local task_prompt
            task_name=$(basename "$markdown_file" .md)
            task_prompt=$(cat "$markdown_file")
            [[ -n "$task_prompt" ]] || {
                echo "错误: Markdown 任务为空: $markdown_file" >&2
                return 1
            }
            tasks_json=$(echo "$tasks_json" | jq \
                --arg name "$task_name" \
                --arg prompt "$task_prompt" \
                '.tasks += [{name:$name,prompt:$prompt}]')
        done < <(find "$MARKDOWN_DIR" -maxdepth 1 -type f -name "$MARKDOWN_PATTERN" -print | sort -V)
    fi

    local append_text="$APPEND_PROMPT"
    if [[ -n "$APPEND_PROMPT_FILE" ]]; then
        local append_file_text
        append_file_text=$(cat "$APPEND_PROMPT_FILE")
        if [[ -n "$append_text" && -n "$append_file_text" ]]; then
            append_text="${append_text}"$'\n\n'"${append_file_text}"
        elif [[ -n "$append_file_text" ]]; then
            append_text="$append_file_text"
        fi
    fi
    if [[ -n "$append_text" ]]; then
        tasks_json=$(echo "$tasks_json" | jq \
            --arg append "$append_text" \
            '.tasks |= map(.prompt = ((.prompt // "") + "\n\n" + $append))')
    fi

    echo "$tasks_json"
}

# 等待任务完成（轮询 tasks/<task_id>.json）
wait_for_task_completion() {
    local task_id=$1
    local timeout=$2
    local task_start_time=$3
    local elapsed=0
    local check_interval=20
    local task_file="${TASKS_DIR}/${task_id}.json"

    WAIT_RESULT=""
    echo "Waiting for task: $task_id (timeout: ${timeout}s)"

    while [ $elapsed -lt $timeout ]; do
        if [ -f "$task_file" ]; then
            local status=$(jq -r '.status // ""' "$task_file" 2>/dev/null || echo "")
            local timestamp=$(jq -r '.completed_at // ""' "$task_file" 2>/dev/null || echo "")
            local run_mode=$(jq -r '.run_mode // ""' "$task_file" 2>/dev/null || echo "")
            local tmux_session=$(jq -r '.tmux_session // ""' "$task_file" 2>/dev/null || echo "")
            local tmux_socket=$(jq -r '.tmux_socket // ""' "$task_file" 2>/dev/null || echo "")

            if { [ "$status" = "starting" ] || [ "$status" = "running" ]; } && [ "$run_mode" = "tmux" ] \
                && [ -n "$tmux_session" ] && [ -n "$tmux_socket" ]; then
                if ! tmux -S "$tmux_socket" has-session -t "$tmux_session" 2>/dev/null; then
                    local orphaned_at="$(date -Iseconds)"
                    jq --arg ts "$orphaned_at" \
                        '. + {status:"failed", lifecycle_stage:"worker_failed", exit_code:1, failure_reason:"tmux session ended before completion hook", completed_at:$ts}' \
                        "$task_file" > "${task_file}.tmp" 2>/dev/null && mv "${task_file}.tmp" "$task_file"
                    WAIT_RESULT="failed"
                    echo "Task orphaned: $task_id (tmux session ended before completion hook)" >&2
                    return 1
                fi
            fi

            if [ "$status" = "done" ]; then
                local result_time=$(date -d "$timestamp" +%s 2>/dev/null || echo 0)
                if [ $result_time -ge $task_start_time ]; then
                    WAIT_RESULT="done"
                    echo "Task completed: $task_id (${elapsed}s)"
                    return 0
                fi
            elif [ "$status" = "failed" ] || [ "$status" = "error" ]; then
                local failure_reason=$(jq -r '.failure_reason // .output // "task reported failure"' "$task_file" 2>/dev/null || echo "task reported failure")
                WAIT_RESULT="failed"
                echo "Task failed: $task_id — ${failure_reason}" >&2
                return 1
            elif [ "$status" = "waiting_input" ]; then
                WAIT_RESULT="waiting_input"
                echo "Task waiting for input: $task_id"
                return 1
            fi
        fi

        sleep $check_interval
        elapsed=$((elapsed + check_interval))

        if [ $((elapsed % 30)) -eq 0 ]; then
            echo "   Still waiting... (${elapsed}s / ${timeout}s)"
        fi
    done

    WAIT_RESULT="timeout"
    echo "Task timeout: $task_id (${timeout}s)"
    return 1
}

# 从 task 文件提取摘要
extract_task_summary() {
    local task_id=$1
    local task_file="${TASKS_DIR}/${task_id}.json"
    if [ ! -f "$task_file" ]; then return; fi

    local output=$(jq -r '.output // ""' "$task_file" 2>/dev/null || echo "")
    if [ ${#output} -gt 500 ]; then
        output="${output:0:500}..."
    fi
    echo "$output"
}

# 执行单个任务
execute_task() {
    local task_name=$1
    local prompt=$2
    local index=$3

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Task [$index/$TOTAL_TASKS]: $task_name"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    local task_start_time=$(date +%s)

    # 构建完整 prompt（链式传递）
    local full_prompt="$prompt"
    if [ $index -gt 1 ] && [ -n "${LAST_PROMPT:-}" ] && [ -n "${LAST_TASK_ID:-}" ]; then
        local task_summary=$(extract_task_summary "$LAST_TASK_ID")
        full_prompt="【上一步任务】
$LAST_PROMPT

【上一步执行结果】
$task_summary

【当前任务】
请先检查上一步是否完成（务必检查git是否已提交），如未完成请继续。如已完成，请继续完成本次工作：
$prompt"
        echo "Chaining from previous task (${#LAST_PROMPT} chars)"
    fi

    # 每个 Part 使用独立 tmux session，避免 Hook 的 session 元数据和
    # current-task-id.txt 在串行任务之间互相覆盖；父级名称仍保留为批次标识。
    local child_tmux_session="${TMUX_SESSION}-p${index}"
    local dispatch_args=(-p "$full_prompt" -n "$task_name" -w "$WORKDIR" --tmux-session "$child_tmux_session")

    echo "   Child tmux session: $child_tmux_session"

    if [ -n "$FEISHU_TARGET" ]; then
        dispatch_args+=(--target "$FEISHU_TARGET")
    fi
    if [ -n "$CDP_PORT" ]; then
        dispatch_args+=(--cdp "$CDP_PORT")
    fi
    if [ -n "$SANDBOX" ]; then
        dispatch_args+=(--sandbox "$SANDBOX")
    fi
    if [ -n "$SKIP_GIT_REPO_CHECK" ]; then
        dispatch_args+=(--skip-git-repo-check)
    fi
    if [ -n "$MODEL" ]; then
        dispatch_args+=(--model "$MODEL")
    fi

    # 执行 dispatch
    local dispatch_output
    dispatch_output=$("$DISPATCH_SCRIPT" "${dispatch_args[@]}" 2>&1)
    echo "$dispatch_output"

    # 提取 Task ID
    local task_id
    task_id=$(echo "$dispatch_output" | grep "Task ID:" | sed 's/.*Task ID: //' | tr -d ' ')

    if [ -z "$task_id" ]; then
        echo "Warning: cannot extract Task ID"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        return 1
    fi

    echo "   Task ID: $task_id"

    # 等待完成
    if wait_for_task_completion "$task_id" "$WAIT_TIMEOUT" "$task_start_time"; then
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        LAST_PROMPT="$prompt"
        LAST_TASK_ID="$task_id"
        return 0
    else
        if [ "$WAIT_RESULT" = "timeout" ]; then
            TIMEOUT_COUNT=$((TIMEOUT_COUNT + 1))
            FAILED_COUNT=$((FAILED_COUNT + 1))
            LAST_PROMPT="$prompt"
            LAST_TASK_ID="$task_id"
            echo "   Timeout recorded as failure; stop-on-error=${STOP_ON_ERROR}"
            return 1
        fi
        FAILED_COUNT=$((FAILED_COUNT + 1))
        LAST_PROMPT="$prompt"
        LAST_TASK_ID="$task_id"
        return 1
    fi
}

main() {
    parse_args "$@"

    echo "Batch Codex dispatch"
    echo "   Tasks input: ${TASKS_FILE:-$MARKDOWN_DIR}"
    echo "   Workdir: $WORKDIR"
    echo "   Feishu: ${FEISHU_TARGET:-none}"
    echo "   Model: ${MODEL:-default}"
    echo "   Tmux session: $TMUX_SESSION"
    echo ""

    local tasks_json
    tasks_json=$(load_tasks_json)
    TOTAL_TASKS=$(echo "$tasks_json" | jq '.tasks | length' 2>/dev/null || echo 0)

    if [ $TOTAL_TASKS -eq 0 ]; then
        echo "Error: no tasks in file" >&2; exit 1
    fi

    echo "Total tasks: $TOTAL_TASKS"
    echo ""

    for i in $(seq 0 $((TOTAL_TASKS - 1))); do
        local task_name
        task_name=$(echo "$tasks_json" | jq -r ".tasks[$i].name" 2>/dev/null || echo "task-$i")
        local prompt
        prompt=$(echo "$tasks_json" | jq -r ".tasks[$i].prompt" 2>/dev/null || echo "")

        if [ -z "$prompt" ]; then
            echo "Skip task $task_name: empty prompt"
            FAILED_COUNT=$((FAILED_COUNT + 1))
            continue
        fi

        if ! execute_task "$task_name" "$prompt" "$((i + 1))"; then
            if [ "$STOP_ON_ERROR" = "true" ]; then
                echo "Task failed, stopping (--stop-on-error)"
                break
            fi
        fi
    done

    local end_time=$(date +%s)
    local total_time=$((end_time - START_TIME))

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Summary: total=$TOTAL_TASKS success=$SUCCESS_COUNT timeout=$TIMEOUT_COUNT failed=$FAILED_COUNT time=${total_time}s"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [ $FAILED_COUNT -gt 0 ]; then exit 1; fi
}

main "$@"
