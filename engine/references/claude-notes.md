# Claude Code CLI 参考笔记（对应 v2.1.329，2026-11-20 整理）

> 历史协议笔记，记录旧入口当时的实现。当前命令与行为以 [cli-manager README](../README.md) 及其中的专题参考为准。

来源：本机 `claude --help`，官方文档 code.claude.com/docs/en/{cli-reference, headless, settings, model-config, env-vars}，以及本机实测。

## 本技能实际使用的调用形态

```
[CLAUDE_CONFIG_DIR=<配置目录>] claude -p --output-format stream-json --verbose \
  [--model M] [--effort E] [--permission-mode ...] [--permission-prompt-tool mcp__cliapproval__permission_prompt --mcp-config <审批通道>] \
  [--allowedTools ...] [--disallowedTools ...] [--append-system-prompt ...]  < prompt
```

- 提示词从 stdin 传入（`-p` 没有位置参数时读 stdin，上限 10MB）。
- 工作目录 = 子进程的 cwd。实测 `system/init` 事件里的 `cwd` 字段与传入的 `--cwd` 一致，脚本会比对并在不一致时警告。

## 宿主环境变量问题（重要）

在 Claude 桌面 App 的会话里，Bash 子进程会继承宿主注入的变量，例如 `ANTHROPIC_BASE_URL`、`CLAUDE_CODE_OAUTH_SCOPES`、`CLAUDE_CODE_ACCOUNT_UUID`、`CLAUDE_CODE_USER_EMAIL`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_MESSAGING_SOCKET/TOKEN`、`CLAUDECODE`、`CLAUDE_CODE_CHILD_SESSION`、`CLAUDE_EFFORT`、`CLAUDE_CODE_SIMPLE`、`MCP_*`、`DISABLE_*` 等。
直接运行 `claude` 可能沿用宿主的账号、网关或会话通道，所以脚本会剔除前缀为 `CLAUDE`、`ANTHROPIC_`、`MCP_`、`OTEL_`、`DISABLE_`、`ENABLE_`、`MAX_THINKING`、`API_TIMEOUT`、`BASH_MAX_` 等的变量，再设置 `CLAUDE_CONFIG_DIR`。
需要保留某个变量时用 `--pass-env NAME`；需要新增时用 `--env K=V`。
另外：设置了 `ANTHROPIC_API_KEY` 时，`-p` 会优先用 API key 而不是登录账号，这也是必须剔除它的原因。

## 模型与思考强度

- `--model`：别名 `fable`、`opus`、`sonnet`、`haiku`、`best`(=opus)、`opusplan`（计划模式用 opus，其余用 sonnet）、`opus[1m]`/`sonnet[1m]`（1M 上下文），或全名 `claude-opus-5-5`、`claude-sonnet-5-5`、`claude-haiku-5-5`、`claude-fable-5-2`。
- 优先级：会话内 `/model` > `--model` > `ANTHROPIC_MODEL` > settings `model`。
- `--effort`：`low | medium | high | xhigh | max | ultracode`（ultracode = xhigh + ultracode 模式）。不持久化。
  - 新模型（Fable 5.2、Opus/Sonnet/Haiku 5.5）默认 medium；不支持的档位会降到不高于它的最高档。
  - 环境变量 `CLAUDE_CODE_EFFORT_LEVEL` 优先级高于 `--effort`，脚本已剔除宿主的该变量。
- `MAX_THINKING_TOKENS`：固定思考预算（脚本参数 `--thinking-tokens`）。自适应思考的新模型基本忽略正数值；Opus/Sonnet/Haiku 5.5 和 Fable 不能关闭思考。
- `--fallback-model a,b`：主模型过载/不可用时按顺序回退，最多 3 个。

## 权限

- `--permission-mode`：`default`/`manual`、`acceptEdits`、`plan`、`auto`、`dontAsk`、`bypassPermissions`。`-p` 下没人设置模式时为 manual。
- `--permission-prompts none`：会弹出批准的请求直接拒绝（不等待宿主），避免非交互运行卡住。
- `--allowedTools` / `--disallowedTools`：逗号或空格分隔的规则，如 `"Bash(git diff *)"`、`"Edit"`、`"mcp__*"`。`Bash(git diff *)` 中星号前的空格有意义。裸 `Bash` 在 auto 模式下会被丢弃。
- `--tools`：限制内置工具集，`""` 全关，`"default"`，或 `"Read,Edit,Bash"`。不影响 MCP 工具。
- `--restricted`：去掉执行代码的工具、忽略用户/项目设置、拒绝 bypass（可通过 `--` 透传）。
- 本技能档位映射（全部默认加 `--disallowedTools "Bash(git push *),Bash(git push),ExitPlanMode,EnterPlanMode,CronCreate,CronDelete,ScheduleWakeup,RemoteTrigger,PushNotification,SendMessage"`，`--allow-push` 可去掉 push 限制）：
  - `read` → `--permission-mode dontAsk --tools Read,Glob,Grep,WebSearch,WebFetch,TodoWrite --allowedTools WebSearch,WebFetch`（不审批）
  - `edit` → `--permission-mode acceptEdits` + 审批通道 + settings 注入 `permissions.ask`（`ASK_RULES`：rm/mv/cp/sed -i/git 改写等）
  - `strict` → `--permission-mode default` + 审批通道（连 Write/Edit 也审批）
  - `auto` → `--permission-mode auto` + 审批通道 + `ASK_RULES`
  - `full` → `--permission-mode bypassPermissions`（需 `--confirm-full`）
  - `--no-approval` → 不接审批通道，改用 `--permission-prompts none`（需批准的直接拒绝，AskUserQuestion 不可用）

## 会话

- `--resume <id|name>`、`--continue`（当前目录最近会话，-p 下也包含 -p 会话）、`--fork-session`、`--session-id <uuid>`、`--name`、`--no-session-persistence`。
- 会话按工作目录存放在 `<CLI 配置目录>/projects/` 下，续聊时要用同一个工作目录。

## 限制

- `--max-turns N`、`--max-budget-usd X`（客户端估算，可能略超）。
- 超时：脚本先发 SIGINT（让当前轮结束），再 SIGTERM、SIGKILL。SIGTERM 退出码 143，当前轮不会完成。

## 输出

stream-json 每行一个事件。关键事件：
- `{"type":"system","subtype":"init", "cwd", "session_id", "model", "tools", "mcp_servers", ...}`
- `{"type":"assistant", "message":{"content":[{"type":"text"}|{"type":"tool_use","name","input"}]}, "parent_tool_use_id"}`（子代理的消息 parent_tool_use_id 非空）
- `{"type":"system","subtype":"api_retry", ...}`
- 最后一行 `{"type":"result", ...}`

result 消息实测字段：`subtype`（success / error_max_turns / error_max_budget_usd / error_during_execution 等）、`is_error`、`result`、`session_id`、`num_turns`、`duration_ms`、`duration_api_ms`、`total_cost_usd`、`usage`、`modelUsage`、`permission_denials`（`tool_name`、`tool_input`）、`terminal_reason`、`stop_reason`、`structured_output`（仅 `--json-schema`）、出错时 `api_error_status`、`api_error_type`、`api_error`、`api_error_origin`，以及 `credential_expires_at`。

注意：额度用尽时 `subtype` 仍是 `success`，但 `is_error=true`、`api_error_status=429`、`api_error_type=rate_limit_error`、`api_error=usage_limit_reached`，`result` 文本类似 "You've hit your 5-hour limit · resets 8:30pm (Asia/Tokyo)"。脚本把这种情况映射为退出码 5。

## 其他可透传的参数（放在 `--` 之后）

`--agents <json|file>`、`--agent <name>`、`--plugin-dir`、`--disable-slash-commands`、`--safe-mode`、`--bare`（不读 OAuth，只能用 API key，不适合本订阅账号）、`--exclude-dynamic-system-prompt-sections`、`--system-prompt[-file]`（替换整个系统提示词，慎用）、`--include-partial-messages`、`--forward-subagent-text`、`-w/--worktree [name]`（在 git worktree 中隔离运行）、`--debug-file <path>`。

## 嵌套

宿主 Bash 中有 `CLAUDECODE=1`、`CLAUDE_CODE_CHILD_SESSION=1`；嵌套的交互式会话不会出现在 `--resume` 列表中，但 `-p` 会话照常保存。脚本剔除这些变量，并用自己的 `CLI_DELEGATE_DEPTH` 防止递归委派。

## 中断与续跑（实测）

- 额度用尽时，会话 jsonl 仍写入 `<CLI 配置目录>/projects/<cwd 编码>/<session_id>.jsonl`：含 user 提示词，以及一条 `isApiErrorMessage: true` 的 assistant 错误消息。
- `claude -p --resume <session_id>` 在同一 session_id 上继续（`system/init` 里的 session_id 不变），新的 user 消息追加到原文件。
- 重置时间只出现在 result 文本里，如 `resets 8:30pm (Asia/Tokyo)`、`resets Nov 24, 3pm (Asia/Tokyo)`。脚本 `parse_reset()` 把它换算成本机时间，存入 meta.json 的 `reset_at`。
- 文档提到的 `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` 可以在 SDK 场景里自动续上中断的轮次；本技能改为显式发一条续跑提示词，行为更可控。

## 运行中的询问与审批通道（实测）

- `--permission-prompts none` 会把 AskUserQuestion 从工具列表里拿掉，需要批准的调用直接拒绝；被拒的调用记录在 result 的 `permission_denials` 里。这是 `--no-approval` 的行为。
- 审批通道：`--mcp-config '{"mcpServers":{"cliapproval":{"command":python3,"args":[approval_mcp.py, RUN_DIR]}}}' --permission-prompt-tool mcp__cliapproval__permission_prompt`。
  - 工具调用的 arguments 实测为 `{"tool_name": "...", "input": {...}, "tool_use_id": "..."}`。
  - 返回值放在 text content 里，是一段 JSON 字符串：批准为 `{"behavior":"allow","updatedInput":{...}}`，拒绝为 `{"behavior":"deny","message":"..."}`。
  - 拒绝时附带的 message 会原样转达给子会话，子会话会照做，不会绕过。
- AskUserQuestion 也走这个通道：`tool_name == "AskUserQuestion"`，`input.questions[]` 里有 question、header、options[label, description]、multiSelect。回答方式是 allow，并带上 `updatedInput = {...input, "answers": {"<问题原文>": "<label 或自由文本>"}}`，多选时用 `", "` 连接。已实测：子会话拿到答案后继续执行。
- `acceptEdits` 档会**自动放行**工作目录内的 `rm`、`mv` 等文件系统命令和复合命令，不经过审批。用 settings 里的 `permissions.ask` 规则（如 `Bash(rm *)`）可以强制这些命令走审批，已实测生效。脚本在 edit / auto 档默认注入 `ASK_RULES`。
- `--permission-mode default`（strict 档）下，Write/Edit 也会走审批通道。
- 审批等待没有 CLI 侧的超时（文档说回调可以一直挂起）。脚本的通道用 `CLI_APPROVAL_TIMEOUT`（默认 1800 秒）兜底，超时按拒绝处理。
- 只靠追加的系统提示词时，haiku 会忽略 `<needs-confirmation>` 格式；在用户消息末尾也加上说明后就能遵守。脚本另有根据结尾问句识别的兜底。
