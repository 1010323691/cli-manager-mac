# 实例管理

需要 Python 3.11+、Git，以及拟使用的 Claude Code 或 Codex CLI。当前执行入口的版本门槛是 macOS、Claude Code 2.1.329、Codex 0.171.x。版本或平台升级的验收项见 [原生协议验证记录](v2-validation.md)。命令中的 `$R` 为 `scripts/delegate.py` 的绝对路径。

## 创建、查询和切换

```bash
python3 "$R" instance create claude1 --backend claude
python3 "$R" instance create codex1 --backend codex --parallel 2
python3 "$R" instance login claude1
python3 "$R" instance login codex1
python3 "$R" instance list
python3 "$R" instance status codex1
```

- 创建时从 `PATH` 找对应 CLI。可用 `--bin` 指定可信的可执行文件绝对路径。实例名以字母或数字开头，最长 80 字符，可用字母、数字、下划线和短横线。实例名按忽略大小写的方式检查重名。实例并发设置 `--parallel` 默认 2，总并发及任务级并发见 [额度调度](scheduling.md#并发与选路)。
- `instance list` 读取已登记实例与缓存状态。`instance status` 强制探测指定实例，返回版本、登录状态、模型、额度、当前运行任务和查询异常。输出省略账户标识。Claude 的模型别名在查询结果中标为未验证。Codex 模型及思考强度来自 `model/list`。
- 登录、退出和切换必须在交互式终端执行，由用户完成原生认证。Claude 使用订阅登录，Codex 只把 ChatGPT 登录识别为可用状态。`switch` 先退出再登录。有正在启动、运行或停止的尝试时，账户操作会拒绝。需要更换账户时用 `instance switch codex1`，只退出时用 `instance logout codex1`。
- 账户身份按「邮箱 + 工作区」区分：Codex 取 `account/read` 的 `workspaceRouting.chatgptAccountId`。同一邮箱的个人空间和 Business 空间可以各绑一个实例，额度与负载分开计算；登录时在授权页选好工作区。Codex 实例的 `instance status` 另返回 `plan`（如 `plus`、`team`）。已按工作区绑定的实例若某次查询不带工作区，判为查询失败，不回退到仅邮箱身份。Codex 登录或切换时 CLI 必须返回工作区才写入绑定；若同邮箱仍有旧键任务在运行，先按邮箱绑定并显示 `rebind_required`，待这些任务结束后再 `instance rebind`。执行会话启动前会在实际的 App Server 上再核对一次账户与工作区，不一致时任务记为 `auth` 并立即使实例探测缓存失效，后续派发或状态查询时才重新探测。
- 早期只按邮箱绑定的 Codex 实例升级后显示 `rebind_required`，暂停派发。实例空闲时运行 `instance rebind codex1`（无需重新登录）绑到 CLI 当前的工作区；重绑推进登录代次，旧代次的排队任务、原生续跑和撞限记录不再沿用，需要的任务重新提交。
- 账户操作会推进实例的登录代次。已绑定旧代次的排队任务不会自动使用新账户。待派发任务可在确认授权后取消并重新提交。已有原生会话的任务按 [续跑与交接](handoff.md) 处理。
- `instance delete` 先返回 `path`、`attempts`、`tasks` 和 `requires_logout`。与未结束任务存在引用或实例仍绑定账户时，确认删除会失败。先检查预览，再按用户授权执行确认命令。

```bash
python3 "$R" instance delete codex1
python3 "$R" instance delete codex1 --confirm
```

## 本地状态

默认目录为 `~/.cli-delegate-v2/`。设置 `CLI_MANAGER_STATE` 可指定目录，其优先级高于旧变量 `CLI_DELEGATE_V2_STATE`。状态目录需由当前用户拥有，权限设为 `0700`。

| 位置 | 内容 |
|---|---|
| `state.sqlite3` | 实例、任务、尝试和审计事件 |
| `instances/<实例>/cli/` | 原生配置与会话目录。Claude 使用 `CLAUDE_CONFIG_DIR`，Codex 使用 `CODEX_HOME` 并选择文件凭据存储 |
| `instances/<实例>/home/`、`cache/`、`tmp/` | 实例独立的主目录、缓存和实例级临时文件 |
| `instances/<实例>/attempts/<尝试>/` | 尝试的工作目录、日志和临时文件 |
| `tasks/<任务>/` | 简报、补丁、审核报告、交接资料和产物 |

执行环境只从宿主继承工具启动所需的环境变量。凭据留在原生存储中。向用户汇报时用实例名、登录状态和任务 ID 即可。

源码对应 [入口与状态输出](../scripts/delegate.py)、[账户生命周期](../scripts/cli_manager/controller.py)、[原生环境与认证](../scripts/cli_manager/backend.py) 和 [状态目录](../scripts/cli_manager/store.py)。
