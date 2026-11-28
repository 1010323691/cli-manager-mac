# 任务与额度调度

命令中的 `$R` 为 `scripts/delegate.py` 的绝对路径。首次使用先看 [实例管理](instances.md)。

## 提交任务

```bash
python3 "$R" task submit --id patch --project "$PWD" --brief "$HOME/task.md" \
  --instance claude1 --handoff-to codex1 --verify 'python3 -m unittest'
python3 "$R" queue work --parallel 4
```

`task submit` 的输入和授权范围以提交时的参数为准。以下表格列出通用选项，图像参数见 [生图](images.md)。

| 参数 | 行为 |
|---|---|
| `--id` | 可指定唯一任务 ID。省略时自动生成 |
| `--brief`、`--prompt`、`--batch-dir` | 必须三选一。前两项接受 UTF-8 简报文件或直接输入，最后一项仅用于生图 |
| `--kind`、`--project` | `code` 为默认值，`review` 用于只读审查，`image` 用于生图。代码与审查任务都要给有效的项目目录 |
| `--instance`、`--backend` | 代码与审查任务至少提供一项授权范围。可重复给实例。按后端提交时，从已登记的匹配实例生成名单，`auto` 范围包含两个后端。显式实例优先，和指定后端冲突时拒绝提交 |
| `--model`、`--effort` | Codex 默认选择该实例模型列表中版本最新的 Sol，思考强度为 `medium`。两个参数可单独指定，只覆盖对应默认值。实例须支持最终组合才会派发。Claude 使用自身默认，显式参数按其别名和强度检查 |
| `--handoff-to` | 可重复给预授权接手的实例。交接手续见 [续跑与交接](handoff.md) |
| `--depends-on` | 可重复给已存在的同项目上游任务 ID。上游审核通过后，下游可以派发 |
| `--include` | 可重复给 Git 根目录相对文件路径，把指定的未提交内容纳入基线。文件处理及工作目录见 [审核与应用](review-and-apply.md#工作目录与基线) |
| `--access` | Codex 任务的能力档位，默认 `open`，见 [能力档位](#能力档位)。`sandbox` 为封闭沙箱，`full` 需要 `--confirm-full` 并由用户在交互式终端输入确认 |
| `--network` | `on`（默认，外网和本机回环）、`loopback`（只允许 127.0.0.1 / localhost）、`off`。`sandbox` 档强制 `off` |
| `--allow-domain` | 可重复。把外网收窄为域名白名单，回环仍可用。只接受不带协议、端口和通配符的域名，且要求 `--network on` |
| `--read-dir`、`--write-dir` | 可重复，绝对路径。追加只读或可写目录。主会话在简报里指定的工作空间用这两个参数授权。不接受根目录、整个主目录、凭据路径和调度状态目录 |
| `--image` | `on`（默认）或 `off`。代码任务里 Codex 自带的生图工具是否可用。`sandbox` 档关闭 |
| `--parallel` | 当前任务可使用的实例并发额度。要明确允许多路集中到同一实例时，在每路提交指定并发数，同时在队列设置足够的总槽位 |
| `--timeout`、`--approval-timeout` | 单位秒，必须为正整数。默认执行超时 3600 秒，单个审批 1800 秒。等待有效的待批请求时暂停执行计时 |
| `--verify`、`--verify-timeout` | 主会话批准时在隔离副本中执行的验证命令及超时，默认 900 秒。验证需要本机 Codex 沙箱 |

队列进程由主会话启动。可以后台运行，随后用另一个终端或工具调用监控。每个状态目录同一时间只有一个队列进程。`queue work --once` 扫描并派发一轮后退出，已启动的尝试继续执行。常驻模式持续处理排队中或等待额度的任务，当两者及活动尝试都为空时退出。

## 能力档位

档位在提交时确定，写入任务记录。`task resume`、`task retry` 和交接沿用。它适用于 Codex 实例；Claude 实例始终使用封闭沙箱，给只有 Claude 实例的任务显式传开放档选项会被拒绝。

```bash
# 默认：开放档、联网、生图
python3 "$R" task submit --id model --project "$PWD" --instance codex1 --brief "$HOME/task.md"
# 追加工作空间，外网只放行 npm
python3 "$R" task submit --id deps --project "$PWD" --instance codex1 --brief "$HOME/task.md" \
  --read-dir "$HOME/Projects/shared-assets" --write-dir "$HOME/Projects/exports" \
  --allow-domain registry.npmjs.org
# 明确要求封闭沙箱
python3 "$R" task submit --id audit --project "$PWD" --instance codex1 --brief "$HOME/task.md" --access sandbox
```

| 档位 | 沙箱内可做 | 越出沙箱的请求 |
|---|---|---|
| `open`（默认） | 读写任务工作树（含带进来的被忽略内容）和 `--write-dir`；只读 `~/.claude/skills`、`~/.cache`、`PATH` 上的工具链、系统框架与应用、`--read-dir`；按 `--network` 联网 | 作为待批请求经 `task watch` 交主会话，`task decide` 批复后按 Codex 原生审批语义执行 |
| `sandbox` | 只读写任务工作树，禁网，不能生图 | 控制器自动拒绝，不送主会话 |
| `full` | 同 `open` | 自动批准，不逐条询问。不可逆操作仍交主会话，凭据仍拒绝 |

- 开放档的子代理使用用户的真实 `HOME`，无头浏览器等桌面程序才能找到登录钥匙串。CLI 自身的登录仍在实例的 `CODEX_HOME`，Git 不读取用户的全局配置，npm、pip、uv 的缓存放在实例目录。`sandbox` 档和 Claude 实例使用实例独立的 `HOME`。
- 项目里被 `.gitignore` 忽略的内容（原图、贴图、`node_modules`）在建立工作树时带进来。同一卷上用 APFS 克隆，不占额外空间，子代理可以在其中写运行产物，原项目不受影响。不同卷时改为指向原项目的只读链接，这时才把原项目主目录授予只读。原项目主目录平时不可读：其中的 `.env`、密钥文件不会进入工作树，也不会被子代理读到。凭据类文件名（`.env`、`*.pem` 等）和 `.claude`、`.codex` 不带入。这些内容不进快照、检查点、补丁和应用。
- 无头浏览器和图形程序无法在沙箱内启动，子代理按规则直接为该命令申请提权。
- `full` 没有关闭沙箱：这样凭据和不可逆操作的底线才能继续生效。它省掉的是逐条审批。

### 安全底线

任何档位都生效。

- 凭据路径拒绝读写：`~/.ssh`、钥匙串、`~/.aws`、`~/.netrc`、`~/.npmrc`、各 CLI 的登录文件、其他实例的 `CODEX_HOME` 与 `CLAUDE_CONFIG_DIR`、调度状态库。沙箱内由系统强制，因为授权目录都不包含这些位置。越界请求若涉及这些路径，控制器直接拒绝并记录 `approval_denied_by_floor`，不送主会话。已获批、在沙箱外运行的命令只能按命令文本和路径拦截，主会话批准前应读完整命令。
- `git push`、发布（npm、cargo、twine、gh release 等）、部署、删除或移走工作树与可写目录之外的文件，每次都要单独批准，`--always` 会被拒绝。
- 待批请求带完整命令、工作目录、理由、涉及的路径和网络目标。超过 `--approval-timeout` 按拒绝处理。被拒绝或超时的请求在同一会话内再次出现时由控制器直接拒绝，记录 `approval_repeat_denied`。
- 每个任务生效的档位、网络范围和授权目录写入任务记录、尝试记录的 `capabilities`、报告、`ledger` 和审计事件 `capabilities_applied`。报告的 `approvals` 列出每条请求及结果。

`instance status` 的 `capabilities` 列出该实例的 Codex 支持与缺失的能力。所需能力缺失时提交报错，调度也不会把任务派给这样的实例，不会改用封闭沙箱代替。本功能之前提交、任务记录里没有档位的任务按封闭沙箱执行。

## 查询额度

```bash
python3 "$R" quota
python3 "$R" quota codex1
python3 "$R" ledger patch
```

- `quota` 强制探测全部或指定实例。Codex 从 App Server 读取额度，并保留多额度桶及主次窗口。Claude 对 2.1.329 的 `/usage` 文本做有限解析，无法确认恢复时间时保留 `reset_text`，`resets_at` 留空。
- `quality: observed` 表示取得了有效窗口，`quality: unknown` 表示当前没有可用窗口。时间戳可用于区分本次查询和上次观测，`last_observation` 是历史结果。把服务端明确禁止使用的标志和窗口百分比分开读，未知数据无法推出实际剩余额度。
- `ledger` 返回尝试、实例、结果、模型、用量、费用、可获得的前后额度、实际生效的能力档位和产物。省略任务 ID 可查看所有记录。某些后端或阶段缺少费用、模型、用量或运行后额度时，相应字段为空。

## 并发与选路

队列默认全局并发为 4，单实例设置默认 2。对带 `--parallel N` 的任务，实例上限取实例设置与该任务并发数中的较大值，全局槽位仍由队列设置。调度器按账户归并同供应商的实例负载与额度，只采用探测身份与绑定账户一致的额度观测。

选路先检查实例名单、登录代次、模型、任务能力、槽位和服务限额。可用候选先按是否有新鲜额度窗口排序，再依次看账户负载、实例负载、最大窗口占用率和实例名。探测与任务收尾共用按工作区账户键保存的额度记录。窗口与服务端禁用标志分别按测量时间更新，迟到的旧值和未知值不覆盖已知测量。一般窗口和禁用标志的新鲜期为 120 秒；有明确重置时间的满额窗口持续阻止派发，直到重置或更新的窗口测量解除满额。额度未知时，同账户已有活动任务便暂停增加并发，除非该任务明确给了大于 1 的 `--parallel`。

运行中撞限会记录 `waiting_quota` 和下一次可尝试时间。队列重新派发时再次检查额度。撞限后的 retry 锁定原实例，换账户须显式交接。主会话有已授权目标时可按 [交接流程](handoff.md#准备和启动交接) 转交。排队停滞先查看 `blocked_by`，它会区分依赖未批准和缺少合格实例。

## 监控、审批和取消

```bash
python3 "$R" task list
python3 "$R" task status patch
python3 "$R" task watch patch --wait 10 --since 0
python3 "$R" task decide patch "$REQUEST_ID" allow
python3 "$R" task decide patch "$REQUEST_ID" allow --always
python3 "$R" task decide patch "$REQUEST_ID" deny --message '超出任务授权'
python3 "$R" task decide patch "$REQUEST_ID" answer --answers '{"question_id":"答案"}'
python3 "$R" task cancel patch
```

- `task watch` 默认最多等 10 秒。出现待批请求、新事件或任务走出运行/等待状态时可以提前返回。用返回的 `since` 作下一次游标，避免反复读取旧事件。轮询时同时检查 `task.state` 和 `task.pending`。
- 审批先读取真实请求，判断用户是否已授权。工具请求使用 `allow` 或 `deny`，提问用 `answer` 或拒绝。答案要覆盖每个问题，键可用问题 ID 或原文，值是字符串或非空字符串数组。Claude 的问题没有 ID 时用原文。凭据请走原生终端登录。
- Claude 的工具批准受工作目录和沙箱范围约束。Codex 开放档的越界请求（`kind` 为 `command`、`file_change` 或 `permissions`）交主会话：`allow` 批准这一次，`allow --always` 让本会话同类请求放行，`deny --message` 拒绝并把理由告诉子代理。请求的 `floor` 为 `irreversible` 时 `session_allow` 是 `false`，只能逐条批准。`sandbox` 档的扩权请求由控制器拒绝。模型提问在各档位都经主会话。过期或已处理的请求无法重复批复。取消正在运行的任务会先请求停止，之后轮询状态确认完成。
- 任务进入 `review` 后读取报告和实际差异。`needs_report` 需要补齐有效结构化报告。`failed`、`auth`、`timed_out` 或 `interrupted` 可从 `task status` 的尝试与报告中诊断，再选择 [续跑或重试](handoff.md)。

源码对应 [入口、游标和输出](../scripts/delegate.py)、[授权与队列](../scripts/cli_manager/controller.py) 和 [额度与审批协议](../scripts/cli_manager/backend.py)。
