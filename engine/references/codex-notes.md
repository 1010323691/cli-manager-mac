# Codex CLI 参考笔记（对应 codex-cli 0.171.0，2026-11-20 整理）

> 历史协议笔记，记录旧入口当时的实现。当前命令与行为以 [cli-manager README](../README.md) 及其中的专题参考为准。

来源：官方文档（developers.openai.com/codex，现已跳转到 learn.chatgpt.com/docs：developer-commands、non-interactive-mode、config-reference、agent-approvals-security、sandboxing、app-server、auth），本机 `codex app-server generate-json-schema` 生成的协议 schema，以及实测。

## 为什么用 app-server 而不是 `codex exec`

`codex exec` 不支持把审批请求交给外部程序处理。`codex app-server`（默认 `--listen stdio://`，每行一个 JSON-RPC 消息，消息里省略 `"jsonrpc"` 字段）会把审批请求作为**服务端发给客户端的请求**发出来，客户端可以逐个批复。本技能的脚本就扮演这个客户端。

## 握手与主流程

1. `initialize`，参数 `{clientInfo:{name,title,version}, capabilities:{experimentalApi:true}}`，然后发通知 `initialized`。
2. `thread/start`，参数 `{cwd, model, sandbox, approvalPolicy, approvalsReviewer, developerInstructions, ephemeral}`。
   - 返回 `{thread:{id,...}, model, reasoningEffort, sandbox, approvalPolicy, cwd, ...}`。
   - 续跑时改用 `thread/resume`，参数 `{threadId, cwd, model, sandbox, approvalPolicy, ...}`。
3. `turn/start`，参数 `{threadId, input:[{type:"text", text}], effort, model, outputSchema}`。
4. 通知：`turn/started`、`item/started`、`item/completed`（以 completed 为准）、`turn/completed`（`turn.status` 取 `completed` | `interrupted` | `failed`，失败时 `turn.error` 里有 `{message, codexErrorInfo}`）、`error`（`{error, willRetry}`）、`thread/tokenUsage/updated`、`account/rateLimits/updated`。
5. `turn/steer`，参数 `{threadId, expectedTurnId, input}`：向进行中的回合追加消息。拒绝审批时，脚本用它把拒绝理由告诉 Codex。实测 Codex 会照做。
6. `turn/interrupt`，参数 `{threadId, turnId}`：超时时用它中断。

## item 类型

ThreadItem 的取值：`userMessage`、`agentMessage{text}`、`reasoning`、`commandExecution{command,cwd,exitCode,aggregatedOutput,status}`、`fileChange{changes:[{path,kind:{type:add|delete|update},diff}],status}`、`mcpToolCall`、`webSearch`、`plan`，等等。

## 服务端请求（审批），以及脚本怎么回

| method | 关键参数 | 回复 |
|---|---|---|
| `item/commandExecution/requestApproval` | `command`、`cwd`、`reason`、`networkApprovalContext`、`proposedExecpolicyAmendment` | `{decision: "accept" \| "acceptForSession" \| "decline" \| "cancel"}`（另有 `acceptWithExecpolicyAmendment` / `applyNetworkPolicyAmendment` 两种对象形式） |
| `item/fileChange/requestApproval` | `itemId`、`reason`、`grantRoot`（diff 要从同一个 itemId 的 `item/started` 里取） | `{decision: accept \| acceptForSession \| decline \| cancel}` |
| `item/permissions/requestApproval` | `permissions{fileSystem,network}`、`reason` | `{permissions: <授予的子集>, scope: "turn" \| "session"}`；拒绝时 `{permissions:{}}` |
| `item/tool/requestUserInput` | `questions[{id, header, question, options[{label,description}], isOther, isSecret}]` | `{answers: {<问题 id>: {answers: ["..."]}}}` |
| `mcpServer/elicitation/request` | — | `{action: "accept" \| "decline" \| "cancel", content}` |

- `decline`：拒绝，但本轮继续；`cancel`：拒绝，并立刻中断本轮。
- request_user_input 默认只在 Plan 模式下可用。用 `-c features.default_mode_request_user_input=true`（该功能还在开发中）可以在默认模式下开启。实测开启后，模型会调用它提问，答案回传后 Codex 会继续执行。

## 档位映射（sandbox / approvalPolicy / approvalsReviewer）

| 档位 | sandbox | approvalPolicy | approvalsReviewer |
|---|---|---|---|
| read | `read-only` | `never` | — |
| edit | `workspace-write` | `on-request` | `user` |
| strict | `read-only` | `on-request` | `user` |
| auto | `workspace-write` | `on-request` | `auto_review` |
| full | `danger-full-access` | `never` | — |

- approvalPolicy 的可选值：`untrusted`（已退役）、`on-request`、`never`，也可以用 `{granular:{...}}` 逐项配置。`on-failure` 已废弃。
- workspace-write 档：工作目录内可以随意读写和执行命令，**不经过审批**；联网（默认关闭）、写工作目录以外、模型主动申请提权时才会发起审批。`--add-dir` 对应 `-c sandbox_workspace_write.writable_roots=[...]`，`--network` 对应 `-c sandbox_workspace_write.network_access=true`。
- read-only 档：写文件和有副作用的命令都要申请提权，所以全部会走审批。实测 Codex 会把“删除 + 新建”合成一条命令来申请；拒绝并说明理由后，它会改成只申请新建。

## 模型与强度

- `model/list` 返回 `data[]`，每项含 `model`、`isDefault`、`defaultReasoningEffort`、`supportedReasoningEfforts[{reasoningEffort}]`。本机实测有 gpt-6.2-sol（默认）、gpt-6.2-astra、gpt-6.1-*、gpt-6-*、gpt-5.6-* 等，强度可选 low / medium / high / xhigh / max / ultra（各模型不同）。
- 强度在 `turn/start` 里用 `effort` 传；对应的配置项是 `model_reasoning_effort`。

## 额度

- `account/rateLimits/read` 和 `account/rateLimits/updated` 返回 `rateLimits{primary, secondary: {usedPercent, windowDurationMins, resetsAt}, rateLimitReachedType, credits, planType}`。
- 回合失败时，`turn.error.codexErrorInfo` 可能是 `usageLimitExceeded`、`rateLimitExceeded`、`serverOverloaded`、`unauthorized`、`contextWindowExceeded`、`sandboxError` 等。脚本把前两种映射为退出码 5，重置时间取已用满的那个窗口的 `resetsAt`。
- **未实测**：额度真正用尽时的完整报错文本（当前账号还没用到上限）。

## 认证与多账号

- 凭据保存在 `$CODEX_HOME/auth.json`（默认 `~/.codex`），也可以由 `cli_auth_credentials_store` 设为 keyring。`codex login status` 查看登录状态。
- CLI、IDE 插件和 ChatGPT 桌面版共用同一个 `~/.codex` 的登录状态。官方没有给出多账号的做法；用不同的 `CODEX_HOME` 分别登录是推断出来的做法，脚本用 `--codex-home` 支持这种用法。
- 设置了 `OPENAI_API_KEY` / `CODEX_API_KEY` 时可能会改为按 API 计费，所以脚本默认剔除这两个变量；确实需要保留时用 `--pass-env`。

## 其他

- `codex exec` 默认是只读沙箱，要求在 Git 仓库里运行（否则需要 `--skip-git-repo-check`），用 `--json` 输出 JSONL 事件；`codex exec resume <id>|--last` 可以续跑。本技能不使用 exec。
- macOS 上用 Seatbelt（`sandbox-exec`）做沙箱。实测从 Claude 桌面版会话的 Bash 里启动没有问题。
