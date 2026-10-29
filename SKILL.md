---
name: codex-dispatch
description: Use this skill when the user wants to delegate a coding, refactor, debugging, documentation, or multi-step development task to OpenAI Codex through the Codex dispatch program. This skill does not execute the task itself; it prepares parameters and calls dispatch-codex.sh so Codex runs independently in tmux with task metadata and completion hook handling.
read_when:
  - 需要把任务委派给 Codex
  - 需要 Codex 在 tmux 会话里独立执行
  - 需要 Claw Remote 面板可观察 Codex 执行现场
  - 需要通过 dispatch-codex.sh 派发开发任务
metadata: {"clawdbot":{"emoji":"X","requires":{"bins":["tmux","jq","python3","codex"]}}}
allowed-tools: Bash(codex-dispatch:*)
---

# Codex Dispatch Skill

**【修改位置 4】本次修改说明：提示词改用独立命名缓冲区；trust/notify 改用进程参数；交互模式真正传递 resume；等待输入时保留 watchdog；通知意图与状态共同提交，终态检查恢复镜像和 outbox。**

**【修改位置 3】本次统一任务代次隔离、状态事务和逐任务通知 outbox；同名派发不再替换活跃会话，TTL 到期只告警，不按名字清理。已有进程不热更新，也不会自动取消旧进程已安排的计时器。**

本 skill 是给后续 agent 用的调用规范。OpenClaw agent 只负责任务发放，真正执行由 Codex 在 tmux 会话中完成。

## 固定流程（必须）

1. 先回复用户：`收到，已将任务委派给 Codex 执行。`
2. 确定参数：`USER_TASK`、`TMUX_SESSION`；需要覆盖默认浏览器时再提供 `CDP_PORT`。任务名默认继承 `TMUX_SESSION`，工作目录默认 `/root`。
3. 调用 `dispatch-codex.sh`，不要直接运行 `codex` 或 `codex exec`。
4. dispatch 会写入 `starting` 状态、启动 tmux、执行 Codex worker/UI 启动门禁并发送启动通知；只有真实 worker 和 prompt 已提交后才转为 `running`。hook 会在任务完成后写结果和发送完成通知；启动失败由 dispatch/watchdog 记录为失败，不得把 tmux 存在误报为运行中。
5. dispatch 命令返回后立即回复用户 tmux 观察命令，不要 attach，不要轮询等待任务完成。

### 状态与通知边界

- **【修改位置 5】每次粘贴使用唯一命名 tmux buffer，加载、粘贴、失败清理均指向该 buffer；不使用或清空其他任务的默认缓冲区。运行器通过本次 Codex 的 `-c` 参数设置项目 trust 和 notify，不修改全局 `config.toml`，默认模型/登录态仍沿用原配置。严格 Part 的 `notify=[]` 覆盖继续有效。**
- **【修改位置 6】所有通知意图先记录在 `task-meta.json.notification_events`；终态写入自动同时记录终态通知意图，再生成镜像和 outbox。重启后的 watchdog 即使遇到终态，也先补齐镜像及缺失的发送回执，再交接未完成的通知。既有回执的 attempts/delivered 状态不重置；未提交的事件不能提前发送。**
- **【修改位置 7】`waiting_input` 不是 watchdog 的退出条件：保留同一 singleton，暂停自动输入及容量重试，不消耗新预算；真实 Hook 回到 running 后继续健康检查。已有旧 watchdog 不会热加载或自动重启。交互 `--resume <session-id/name>` 明确执行 `codex resume <session-id/name>`，不落入选择器，也不覆盖仍存在的同名 tmux。**
- `task_state.py` 是 runner、Hook、watchdog 的唯一状态写入口。`task-meta.json` 为权威，镜像带同一 revision；事务锁覆盖重新读取、更新、提交与镜像。进程中断后的镜像在下一次事务中修复，不把多文件更新声称为同一瞬间原子可见。
- task_id、Codex thread 和 tmux 实例身份必须匹配；旧回调、旧 watchdog 不得操作同名新任务。done/failed/error 为不可逆终态，迟到进度不能重新打开任务。重复 turn-id 幂等忽略。
- 同名活跃/等待任务或仍存在的 tmux 会话会拒绝重新派发；授权续跑应使用原任务会话和检查点。不要为绕过拒绝而删除元数据、清理旧会话或生成新 ID。headless 任务使用各自的状态目录。
- `SESSION_TTL`（默认 43200 秒）只产生一次健康告警，保留会话；不安排 kill-session。业务结束和实例清理是不同动作，清理需要针对已确认的具体实例另行授权。
- 通知按 task_id + 事件 + 通道 + 收件目标去重，回执位于 `data/outbox/*.json`。入队不等于送达；只有通道命令退出 0 才写 delivered_at。每条最多 3 次尝试，失败保留退出码，等待 5/10 秒后重试；不因其他任务通知而静默丢弃。
- 意外退出后，可对**原回执**运行 `python3 task_state.py drain --receipt <absolute-outbox-json>`，保留尝试次数。已送达或耗尽的不再发送；不要删回执重置预算。发送方崩溃在远端接受与本地登记之间时可能重复送达，不宣称网络 exactly-once。watchdog 终态检查也会尝试交接未完成的终态回执。

## 参数规则

必填参数：

- `USER_TASK`：用户要 Codex 完成的完整任务描述。

推荐自动生成：

- `TMUX_SESSION`：tmux 会话名；建议使用短横线命名，例如 `codex-fix-login-bug`。不传 `-n` 时，dispatch 会自动把它作为任务名。

可选参数：

- `FEISHU_TARGET`：显式覆盖通知目标；不传时按 `CDP_PORT` 映射，无法映射则使用配置中的 `default_target`。
- `WORKDIR`：仅任务不能在默认 `/root` 下执行时覆盖。
- `CDP_PORT`：可选的浏览器端口覆盖值。不传时使用 `dispatch-config.json` 中的 `default_cdp`；通过 `--cdp` 传入后，dispatch 会覆盖默认值，并自动注入 Prompt、环境变量和任务元数据。
- `MODEL`：只在用户明确要求临时换模型时传 `--model`。
- `SANDBOX`：Codex sandbox 模式；不传则使用 Codex 自身配置。
- `RESUME_SESSION_ID`：需要续跑指定 Codex 会话时传 `--resume`。
- `SKIP_GIT_REPO_CHECK`：非 git 目录运行时设为 `1`，脚本会追加 `--skip-git-repo-check`。

## 执行命令

后续 agent 按下面模板构造命令。默认安装路径是 `/root/.openclaw/skills/codex-dispatch`。

```bash
#!/bin/bash
set -euo pipefail

CODEX_DISPATCH_ROOT="${CODEX_DISPATCH_ROOT:-/root/.openclaw/skills/codex-dispatch}"

USER_TASK="${USER_TASK:?需要提供任务描述}"
TMUX_SESSION="${TMUX_SESSION:?需要提供 tmux 会话名}"

FEISHU_TARGET="${FEISHU_TARGET:-}"
CDP_PORT="${CDP_PORT:-}"
MODEL="${MODEL:-}"
SANDBOX="${SANDBOX:-}"
RESUME_SESSION_ID="${RESUME_SESSION_ID:-}"
SKIP_GIT_REPO_CHECK="${SKIP_GIT_REPO_CHECK:-}"

PROMPT="$(cat <<EOF
${USER_TASK}

执行要求：
1. 在 dispatch 默认工作目录 /root 内完成任务。
2. 修改前先阅读相关文件，保持改动范围最小。
3. 如需验证，运行与本次改动直接相关的命令。
4. 完成后输出：改了什么、验证结果、遗留风险。
5. 如果失败，说明失败原因、已完成部分、下一步建议。
EOF
)"

CMD=(
  env -u CLAUDECODE
  "${CODEX_DISPATCH_ROOT}/dispatch-codex.sh"
  -p "$PROMPT"
  --tmux-session "$TMUX_SESSION"
)

if [ -n "$FEISHU_TARGET" ]; then
  CMD+=(--target "$FEISHU_TARGET")
fi

if [ -n "$CDP_PORT" ]; then
  CMD+=(--cdp "$CDP_PORT")
fi

if [ -n "$MODEL" ]; then
  CMD+=(--model "$MODEL")
fi

if [ -n "$SANDBOX" ]; then
  CMD+=(--sandbox "$SANDBOX")
fi

if [ -n "$RESUME_SESSION_ID" ]; then
  CMD+=(--resume "$RESUME_SESSION_ID")
fi

if [ -n "$SKIP_GIT_REPO_CHECK" ]; then
  CMD+=(--skip-git-repo-check)
fi

"${CMD[@]}"
```

## Codex 执行指令（传给 `-p` 的 prompt）

传给 `-p` 的 prompt 必须包含：

```text
任务目标：
<用户原始任务>

工作目录：
<WORKDIR>

执行要求：
1. 先阅读相关文件，确认项目结构和现有约定。
2. 只修改完成任务必须修改的文件。
3. 不要生成无关测试、报告或总结文档，除非用户明确要求。
4. 如需验证，只运行与本次改动直接相关的命令。
5. 完成后汇总：改动文件、改动内容、验证命令和结果、风险或未完成项。
```

## 返回给用户

dispatch 成功启动后，立即告诉用户：

```text
任务已进入 Codex tmux 会话：
tmux -S /root/clawdbot-tmux-sockets/codex-code.sock attach -t <TMUX_SESSION>

按 Ctrl+B 然后按 D 可以退出观察，不会终止任务。
```

派发器默认关闭 Codex 启动时的自更新检查，并对“更新后需要重启”只做一次同会话重启。除维护或无副作用烟测外，不要传 `codex_run.py --allow-update-check`。

tmux watchdog 对明确的 `Selected model is at capacity` + `Please try a different model`（允许相邻换行）提示，在同一 Codex 会话发送明确续跑指令；仅写入 tmux 不算恢复。每轮默认最多 3 次发送，不切模型、不重启业务、不重跑阶段；耗尽后 `waiting_input` 并告警。具体确认与退避规则见下文。可用 `CODEX_DISPATCH_CAPACITY_RETRIES`、`CODEX_DISPATCH_CAPACITY_DELAY_S`、`CODEX_DISPATCH_CAPACITY_MAX_DELAY_S`、`CODEX_DISPATCH_CAPACITY_RESPONSE_TIMEOUT_S` 和 `CODEX_DISPATCH_CAPACITY_INPUT` 调整。

## 关键文件路径

**【修改位置 1】容量确认必须相对发送前基线：新工作标记或发送后的真实工具回合回执才算接受；错误消失、输入回显、旧 Working 计时变化都不算。面板变化不能跳过退避。默认最多 3 次发送，每次等响应 30 秒；前两次无响应后分别等 30/60 秒再发，第三次仍失败则 waiting_input。发送失败也占有界发送预算，但绝不是成功次数。既有 watchdog 不会热加载或被自动重启。**

**【修改位置 2】交互式 Hook 的回合结束不是任务结束：换行 JSON、title、status=running 和未知进度文本均保持 running，不因子步骤成功、提交或勾号退出 watchdog。最终回复必须是单个顶层 JSON（无 Markdown 围栏），含 summary；ok=true 且 status=success/success_with_warnings 才确认成功，顶层失败优先。派发器已注入该输出契约。session、tasks 和 latest 三份记录使用同一状态规则；running/waiting_input 不保留 completed_at 或 exit_code。headless 已退出进程的非终态 JSON 进入 waiting_input，旧文本输出仍按实际退出码处理。**

- Codex dispatch：`/root/.openclaw/skills/codex-dispatch/dispatch-codex.sh`
- Codex runner：`/root/.openclaw/skills/codex-dispatch/codex_run.py`
- Codex hook：`/root/.openclaw/skills/codex-dispatch/hooks/notify-agi.sh`
- tmux socket：`/root/clawdbot-tmux-sockets/codex-code.sock`
- Codex 任务状态：`/root/.openclaw/skills/codex-dispatch/data/`
