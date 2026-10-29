# Codex Dispatch

**【修改位置 2】本次修改说明：配置不再写全局文件，提示词使用独立命名 buffer；交互 resume、等待输入后的持续监控、通知意图与状态镜像的崩溃恢复已补齐。详见 SKILL.md 的状态与通知边界。**

`codex-dispatch/` 是独立的 Codex 任务派发目录。它负责把任务送进 Codex tmux 会话，让学生可以在 tmux 或 Claw Remote 面板里同步观察执行现场。

## 目录结构

```text
codex-dispatch/
  SKILL.md                    # Codex dispatch skill：告诉后续 agent 怎么调用派发程序
  dispatch-config.json        # 默认消息群与 CDP → 飞书群映射
  dispatch-codex.sh            # 单任务派发入口
  batch-dispatch-codex.sh      # 批量串行派发入口
  codex_run.py                 # Codex 运行器
  hooks/notify-agi.sh          # Codex 完成通知 hook
```

## 学生需要准备什么

必需：

- `tmux`
- `jq`
- `python3`
- `codex` CLI，并且学生自己已经登录 / 配置好 Codex。

安装系统依赖：

```bash
apt-get update
apt-get install -y tmux jq python3 git
```

确认 Codex 可用：

```bash
command -v codex
codex --version
codex
```

**【修改位置 3】Codex hook 使用本次进程的 `-c notify=[...]` 参数，真实路径由运行器位置确定；项目 trust 也使用进程级覆盖。无需创建或修改 `~/.codex/config.toml`，已有全局配置保持原样。Hook 会记录任务状态和通知意图，outbox 只在发送成功后登记送达。严格 Part 通过最后的 `-c notify=[]` 禁用父任务回调。启动前只需检查 Hook 可执行：**

```bash
test -x /root/.openclaw/skills/codex-dispatch/hooks/notify-agi.sh
```

默认不需要 `.env`。不传 `--model` 时，脚本直接使用学生自己 `~/.codex/config.toml` / Codex 登录态里的默认模型配置；只有显式传 `--model` 时，才临时覆盖本次任务模型。

## 安装位置

推荐固定放到 OpenClaw skills 目录下：

```text
/root/.openclaw/skills/codex-dispatch
```

确认脚本可执行：

```bash
cd /root/.openclaw/skills/codex-dispatch
chmod +x *.sh *.py hooks/*.sh
```

## 安装后先配置消息群

首次派发任务前，必须打开当前项目目录里的配置文件：

```text
/root/.openclaw/skills/codex-dispatch/dispatch-config.json
```

默认内容使用课堂占位值，不能直接照搬。把其中的飞书 `chat_id` 全部替换成学生自己的真实群 ID：

```json
{
  "default_cdp": "9222",
  "default_target": "oc_xxxx_notification",
  "cdp_targets": {
    "9222": "oc_xxxx_notification",
    "9223": "oc_xxxx_demand",
    "9224": "oc_xxxx_code_1",
    "9225": "oc_xxxx_code_2",
    "9226": "oc_xxxx_code_3"
  }
}
```

- `default_cdp`：默认浏览器端口；未传 `--cdp` 时使用该端口，显式传入时覆盖默认值；
- `default_target`：默认工位的消息通知群；与 `default_cdp` 组成默认浏览器和通知组合；
- `cdp_targets`：CDP 端口与飞书群的对应关系；只传 `--cdp 9226` 时，dispatch 自动使用 `9226` 对应的群；
- `--target`：可选的临时覆盖；只有某次任务要改发其他群时才需要传。

`cdp_targets[default_cdp]` 必须与 `default_target` 相同；本例即 `9222 → oc_xxxx_notification`。

检查 JSON 格式：

```bash
cd /root/.openclaw/skills/codex-dispatch
jq empty dispatch-config.json
jq '.default_cdp, .default_target, .cdp_targets' dispatch-config.json
```

必须看到自己的真实群 ID，不能仍然是 `oc_xxxx_*`。配置完成后，dispatch 的选择顺序是：

```text
显式 --target
→ 否则按 --cdp 查询 cdp_targets
→ 否则使用 default_target
```

## 最小启动

下面不传 `--target`，用于验证刚才配置的 `default_target` 是否生效。

```bash
cd /root/.openclaw/skills/codex-dispatch

env -u CLAUDECODE ./dispatch-codex.sh \
  --tmux-session demo-codex-readme \
  --workdir /root/Documents/dispatch-demo/codex \
  -p "在当前目录创建 README.md，内容写一行 hello from codex dispatch。完成后汇总结果。"
```

查看 tmux 现场：

```bash
tmux -S /root/clawdbot-tmux-sockets/codex-code.sock \
  attach -t demo-codex-readme
```

退出观察但不终止任务：

```text
Ctrl+b，然后按 d
```

指定模型示例：

```bash
env -u CLAUDECODE ./dispatch-codex.sh \
  --tmux-session demo-codex-model \
  --workdir /root/Documents/dispatch-demo/codex \
  --model "<模型名>" \
  --cdp 9226 \
  -p "在当前目录创建 README.md，内容写一行 hello from codex model override。完成后汇总结果。"
```

## `dispatch-codex.sh`

Codex 单任务派发入口。课堂里最优先讲这个文件。

必填参数：

- `-p, --prompt TEXT`：必填。要交给 Codex 执行的任务提示词。

可选参数：

- `-n, --name NAME`：任务名，用于生成 `TASK_ID` 和查看日志；不传时默认使用 `--tmux-session` 的值。
- `-g, --group, --target ID`：可选。显式覆盖通知目标；不传时按 CDP 映射，无法映射则使用 `dispatch-config.json` 的默认消息通知群。
- `--cdp PORT`：可选。浏览器 CDP 端口；不传时读取 `default_cdp`，显式传入时覆盖默认值，并自动写入 Prompt、环境变量和任务元数据。
- `-s, --session KEY`：回调 session key。
- `-w, --workdir DIR` / `--workdir DIR`：可选覆盖 Codex 的工作目录；默认 `/root`，普通任务无需传。
- `--sandbox MODE`：沙箱模式，例如 `read-only`、`workspace-write`、`danger-full-access`；不传则使用 Codex 自身配置。
- `--yolo`：使用 `--dangerously-bypass-approvals-and-sandbox`；当前脚本默认开启，适合课堂可信工作目录。
- `--skip-git-repo-check`：允许在非 git 仓库中运行。
- `--resume SESSION_ID`：续跑指定 Codex session / thread。
- `--model MODEL`：临时覆盖 Codex 模型；不传就用学生自己的默认模型配置。
- `--tmux`：使用 tmux 交互模式；当前默认就是 tmux。
- `--no-tmux`：禁用 tmux，改用 headless 模式。
- `--tmux-session NAME`：指定 tmux 会话名；默认 `codex-agent`。

它负责：

- 解析任务、工作目录、模型、sandbox、tmux 会话等参数；
- 生成 `TASK_ID`；
- 写入 `data/` 下的任务状态文件；
- 把长 prompt 写入 `data/prompts/<TASK_ID>.md`，再让 Codex 读取该文件；
- 设置 hook 需要的 `CODING_AGENT_*` 环境变量；
- 调用 `codex_run.py` 把 Codex 启动进 tmux；
- headless 模式下直接调用 `hooks/notify-agi.sh` 写结果；

## 任务启动门禁与故障恢复

派发状态不是“tmux 存在就算运行”。单任务状态按以下生命周期记录：

```text
starting → worker_started → business_started/running → completed
                                         └──────────→ failed
```

- 派发默认向 Codex 注入 `-c check_for_update_on_startup=false`，避免业务任务启动时修改 Codex 安装。
- 如果旧版 Codex 仍输出“Please restart Codex”，只允许在同一 tmux 会话中自动重启一次；第二次失败即记录 `startup_failed`。
- 启动成功必须同时看到 Codex 子进程和 Codex UI，提交 prompt 后再次确认 worker 仍存在；shell/tmux 存在本身不能放行。
- 每个 tmux 任务会启动 `watchdog.py`，记录心跳、worker PID、pane 活跃度；tmux 消失或 worker 长时间消失会标记失败并通知，但不会盲目重跑建站、广告或部署任务。
- watchdog 默认启动门限为 120 秒、worker 消失宽限 30 秒、静默告警 900 秒、检查间隔 15 秒；可用 `CODEX_DISPATCH_STARTUP_TIMEOUT`、`CODEX_DISPATCH_WORKER_MISSING_GRACE`、`CODEX_DISPATCH_HEARTBEAT_TIMEOUT` 和 `CODEX_DISPATCH_WATCHDOG_INTERVAL` 调整。
- watchdog 识别到明确容量提示后，在原 tmux 会话发送明确续跑指令并等待真实接受证据；仅 `send-keys` 成功不计为恢复。每轮默认最多 3 次发送，不换模型、不重启 worker、不重跑业务阶段；耗尽后 `waiting_input` 并告警。具体响应窗口与退避规则见下文。可用 `CODEX_DISPATCH_CAPACITY_RETRIES`、`CODEX_DISPATCH_CAPACITY_DELAY_S`、`CODEX_DISPATCH_CAPACITY_MAX_DELAY_S`、`CODEX_DISPATCH_CAPACITY_RESPONSE_TIMEOUT_S` 和 `CODEX_DISPATCH_CAPACITY_INPUT` 调整。
- stdout/stderr、watchdog 日志和状态文件按会话隔离，避免并发任务覆盖诊断证据。
- 仅维护/烟测场景可显式给 `codex_run.py` 传 `--allow-update-check`；生产派发不应使用该逃生参数。

关键证据文件：

**【修改位置 1】容量重试按发送前基线确认：新出现的 Codex 工作标记或发送后的真实工具回合回执才算接受。错误文字消失、输入回显、旧 Working 的计时变化不算；面板变化不能提前结束退避。最多发送 3 次（发送失败/无响应也占发送预算，但不是成功次数），每次等待响应默认 30 秒，再按 30/60 秒退避后发下一次；第三次仍失败即 waiting_input。不会切模型或自动重启旧 watchdog。修改只由后续新启动的 watchdog 加载。**

- `data/sessions/<tmux-session>/task-meta.json`
- `data/sessions/<tmux-session>/task-output.txt`
- `data/sessions/<tmux-session>/watchdog.log`
- `data/tasks/<task-id>.json`
- tmux 模式下依赖 Codex notify hook 完成结果回写。

## `codex_run.py`

Codex 运行器。它不是课堂主入口，主要被 `dispatch-codex.sh` 调用。

必填参数：

- 无。独立运行时也可以不传参数；dispatch 流程会自动传入必要参数。

可选参数：

- `-p, --prompt TEXT`：要发送给 Codex 的 prompt。
- `--mode auto|headless|tmux`：运行模式；dispatch 默认传 `tmux`。
- `--cwd DIR`：Codex 工作目录。
- `--sandbox MODE`：透传 Codex sandbox 模式。
- `--yolo`：透传 Codex bypass approvals / sandbox 参数。
- `--skip-git-repo-check`：允许非 git 仓库运行。
- `--resume SESSION_ID`：恢复指定 Codex session / thread。
- `--model MODEL`：模型覆盖；不传就使用 Codex 自身默认配置。
- `--codex-bin PATH`：Codex CLI 路径；默认读取 `CODEX_BIN`，再查找 `codex`。
- `--tmux-session NAME`：tmux 会话名；默认 `codex-agent`。
- `--tmux-socket-dir DIR`：tmux socket 目录；默认 `/root/clawdbot-tmux-sockets`。
- `--tmux-socket-name NAME`：tmux socket 文件名；默认 `codex-code.sock`。
- `--interactive-wait-s SECONDS`：interactive 启动后等待 N 秒再打印 tmux 快照。
- `--output-file FILE`：headless 模式下最后消息输出文件。
- `-- EXTRA_ARGS`：透传给 Codex CLI 的额外参数。

它负责：

- 找到 `codex` CLI；
- 检查并补充 Codex notify 配置；
- 构造 headless 或 tmux interactive 命令；
- 创建 / 复用 / 清理 tmux 会话；
- 把 Codex 启动在指定 tmux 会话里；
- 把任务环境变量写进 tmux server 和 shell；
- 把 prompt 粘贴进 Codex 输入框并回车；
- 输出 attach / capture-pane 命令，方便人工观察。

## `batch-dispatch-codex.sh`

Codex 批量串行派发脚本。它可以读取 `tasks.json`，也可以直接读取一个 Markdown 任务目录，然后按顺序把多个任务逐个派给 Codex。

任务输入二选一：

- `--tasks FILE`：读取包含 `tasks` 数组的 JSON 任务文件。
- `--markdown-dir DIR`：读取目录中的 Markdown 任务，文件名去掉 `.md` 后作为任务名，正文作为 Prompt。

可选参数：

- `--markdown-pattern GLOB`：Markdown 任务文件匹配规则；默认 `part*.md`，并按文件名自然排序。
- `--append-prompt TEXT`：在每个任务 Prompt 末尾追加同一段较短要求。
- `--append-prompt-file FILE`：在每个任务 Prompt 末尾追加指定文件的完整内容。Markdown 模式下，未显式传入时自动读取同目录的 `common-requirements.md`。
- `-g, --group, --target ID`：可选覆盖通知目标；不传时由每个 Part 的单任务 dispatch 根据配置自动选择。
- `--cdp PORT`：浏览器 CDP 端口，自动传给每个 Part。
- `-w, --workdir DIR` / `--workdir DIR`：所有子任务共用的工作目录；默认 `/root`，仅需改到其他目录时传。
- `--sandbox MODE`：传给每个 Codex 子任务的 sandbox 模式。
- `--skip-git-repo-check`：允许在非 git 仓库中运行。
- `--model MODEL`：传给每个 Codex 子任务的模型覆盖值。
- `--wait-timeout SECONDS`：单个 part 最长等待时间；默认 `3600` 秒。
- `--stop-on-error`：某个 part 失败时立即停止；默认继续后面的任务。
- `--tmux-session NAME`：基础 tmux 会话名前缀；每个 part 会变成 `${NAME}-p<序号>`。
- `-h, --help`：打印帮助。

`tasks.json` 格式：

```json
{
  "tasks": [
    {"name": "part-1", "prompt": "第一阶段任务"},
    {"name": "part-2", "prompt": "第二阶段任务"}
  ]
}
```

它负责：

- 读取 `--tasks tasks.json` 或 `--markdown-dir DIR`；
- 合并 `--append-prompt`、`--append-prompt-file` 或默认的 `common-requirements.md`；
- 校验任务列表格式；
- 给每个 part 派一个独立 tmux 会话：`${TMUX_SESSION}-p<序号>`；
- 调用 `dispatch-codex.sh` 派发当前 part；
- 从 dispatch 输出里提取 `TASK_ID`；
- 轮询 `data/tasks/<TASK_ID>.json` 等待任务完成；
- 把上一步结果摘要传给下一步，形成链式上下文；
- 最后输出成功、失败、超时和总耗时。

## `hooks/notify-agi.sh`

Codex 完成通知 hook。Codex tmux 模式下，任务完成后由 Codex notify 调用它。

可选输入：

- `$1`：Codex notify 传入的 JSON payload；交互式 tmux 模式主要从这里解析最后输出、thread id 和工作目录。
- `CODING_AGENT_SESSION_DIR`：dispatch 注入。当前任务所在 session 状态目录；默认回退到 `data/`。
- `CODEX_EXIT_CODE`：headless 模式注入。Codex 进程退出码；默认 `0`。
- `CODEX_LAST_MSG_FILE`：headless 模式注入。Codex 最后一条消息文件。
- `CODEX_JSONL_FILE`：Codex JSONL 输出文件，用于提取 thread id。
- `CODEX_TASK_OUTPUT`：headless 模式注入。完整任务输出文件；默认 `data/task-output.txt`。
- `FEISHU_TARGET`：可选覆盖通知目标；默认由 `dispatch-config.json` 根据 CDP 或默认消息通知群自动选择。

它负责：

- 读取 Codex notify payload 或 headless 环境变量；
- 提取最后输出、thread id、工作目录；
- 找到对应 `task-meta.json`；
- 写入 `data/tasks/<TASK_ID>.json`；
- 写入 `data/latest.json`；
- 更新 `task-meta.json` 的 `status` / `completed_at`；
- 做简单去重，避免重复通知；
- 有真实通知配置时发送飞书完成通知。

## Claw Remote 观察

Codex dispatch 固定使用：

```text
/root/clawdbot-tmux-sockets/codex-code.sock
```

只要 Claw Remote 后端读取同一个 socket，就能看到 Codex 任务现场。

```bash
tmux -S /root/clawdbot-tmux-sockets/codex-code.sock list-sessions
```

任务状态文件：

```text
/root/.openclaw/skills/codex-dispatch/data/
```
