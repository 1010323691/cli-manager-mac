> 历史资料。下文记录旧入口 `scripts/cli_delegate.py`，用于查询或收尾旧任务。当前使用方式见 [cli-manager README](../../README.md)。

# codex-cli：在 Claude 会话里调用 Codex CLI 处理任务

后端脚本位于 `~/.claude/skills/cli-manager/backends/codex/`，但**一律经统一入口调用**：下文 `$X` 指 `~/.claude/skills/cli-manager/scripts/cli_delegate.py`，其中 `run` 要加 `--backend codex`。

Codex 发来的审批请求和提问，会写到运行记录的 `approvals/` 目录，等主会话用 `decide` 批复。脚本还负责清理宿主会话注入的环境变量（包括 `OPENAI_API_KEY`），记录完整事件流，并打印中文摘要。

协议和配置细节见 [../../references/codex-notes.md](../../references/codex-notes.md)，排查问题或需要透传配置时再读。

## 调用方式：主会话直接运行

主会话自己通过统一入口运行脚本，不再派包装子智能体：启动用 `run_in_background: true` 放后台，再循环 `watch` → 审核 → `decide`。完整的主会话流程（写简报、选参数、审批、推送通知、续跑、汇报）见 [../../SKILL.md](../../SKILL.md)「调用方式：主会话直接运行」，下文第 1–8 步是各步骤的细节。

**注意：** 只通过统一入口 `scripts/cli_delegate.py` 调用，**不要直接调用本后端脚本**，也不要为它整体放行（`delegate.py *` / `codex_delegate.py *` 这类规则会把敏感参数一并放开）。权限放行按 SKILL.md「配置与安装」的子命令清单逐条配置。

## 硬性约束

1. **工作目录必须与调用会话相同**：始终显式传 `--cwd "<本会话的主工作目录>"`（系统提示 Environment 里的 *Primary working directory*，或用户切换后的项目目录）。不要依赖 Bash 当前的 `cd` 位置，不要传 scratchpad。需要额外可写目录时用 `--add-dir`。
2. **只在用户要求时调用**，会消耗用户的 Codex 额度。
3. **审批档位（edit / strict 等）必须后台运行，并循环执行 watch → 审核 → decide**（第 4、6 步）。前台运行会死锁。由主会话负责这个循环。
4. **审批分层负责**：主会话只自行处理白名单内的请求（任务范围内的安全操作），以及明显越界、应当拒绝的请求，其余按第 6 步的规则判断，拿不准的才问用户。
5. **`--access full`（关闭沙箱且不审批）只能在用户本轮明确同意后使用**，并且要同时加 `--confirm-full`。`--network`（沙箱内允许联网）也要在用户授权范围内才能用。
6. **子会话运行期间，本会话不要改动它可能改动的文件**；并行跑多个委派时，各自负责的文件不能重叠。
7. **不要把密码、密钥、token 写进提示词**；审批时也不要提供这些内容。
8. **结果不是事实**：子会话的报告要核实（`git diff`、读改动的文件、跑测试）后，才能告诉用户「完成了」。
9. **额度用尽（退出码 5）或超时（退出码 3）时，任务没有丢**：线程已保存，按第 7 步在同一线程上续跑。不要立刻反复重试。
10. 禁止嵌套：子会话里再调用本技能或 claude-cli 都会被脚本拒绝（两个技能共用 `CLI_DELEGATE_DEPTH` 计层）。

## 流程

### 1. 体检（每个会话第一次用时）

```bash
python3 ~/.claude/skills/cli-manager/scripts/cli_delegate.py doctor
```

会显示 CLI 版本、登录状态、可用模型及其支持的思考强度、当前额度。看到 `结论: 可用` 才继续。未登录时，让用户在终端运行 `codex login`（只能用户自己登录）。要用另一个账号，用户先执行 `CODEX_HOME=~/.codex-account2 codex login`，之后调用时加 `--codex-home ~/.codex-account2`。

### 2. 写任务简报

Codex **看不到本对话的任何上下文**，简报必须能独立看懂。写到 scratchpad 的文件里，用 `--prompt-file` 传入。简报包含：

- 目标和完成标准
- 范围：动哪些文件或目录，**哪些不能动**
- 已知信息：已经查明的原因、相关路径、项目约定、测试/构建命令
- 交付：最终报告里要列出什么

审批时要以这份简报划定的范围为依据。

### 3. 选参数

| 用户说的 | 参数 |
|---|---|
| 指定模型 | `-m <模型 id>`（按 `doctor` 列出的 id，如 `gpt-6.2-sol`）；不指定就用账号默认模型 |
| 思考强度 | `-e low/medium/high/xhigh/max/ultra`（以 `doctor` 显示的该模型支持范围为准）；不指定就用模型默认值 |
| 只看不改：分析、调研、审查 | `-a read`：只读沙箱，不审批，越权操作直接失败 |
| 一般的改代码任务 | `-a edit`（默认）：工作目录可写；越出沙箱的操作（联网、写工作目录以外、模型主动申请提权）和提问交给主会话 |
| 要求每一步都审，尤其涉及删除或改动已有文件时 | `-a strict`：只读沙箱，所有写文件、改文件、执行有副作用的命令都交给主会话审批 |
| 用户同意让 Codex 自己审 | `-a auto`：工作目录可写，提权请求由 Codex 内置的自动审查员判断，不经过主会话 |
| 用户明确说全部放开 | `-a full --confirm-full` |
| 无人值守 | `--no-approval`：越出沙箱的操作直接失败，也不开启提问工具 |
| 限制时长 | `--timeout 秒`（默认 3600，超时会中断本轮）、`--approval-timeout 秒`（单个审批等待上限，默认 1800，超时按拒绝处理） |
| 要结构化结果 | `--json-schema '<schema 或文件>'` |
| 需要透传 Codex 配置 | `-c key=value`（可重复，例如 `-c model_verbosity=low`） |

**注意 `edit` 档的局限**：Codex 的 workspace-write 沙箱允许在工作目录内执行 `rm`、`mv`、`git` 等命令，**不会触发审批**；Codex 没有 Claude 那种“强制询问”规则可以注入。如果任务涉及用户已有的重要文件，或者用户要求删改都要审，用 `strict`。

拿不准时先加 `--dry-run` 看最终参数。

### 4. 运行

审批档位（edit / strict）用 Bash 的 `run_in_background: true` 启动，并用 `--id` 给这次运行起个名字：

```bash
python3 ~/.claude/skills/cli-manager/scripts/cli_delegate.py run --backend codex --id <任务名> \
  --cwd "<主工作目录>" --prompt-file "<scratchpad>/task.md" -m gpt-6.2-sol -e high
```

然后在前台**循环**执行 `watch`（Bash timeout 设为 600000）：

```bash
python3 ~/.claude/skills/cli-manager/scripts/cli_delegate.py watch <任务名>
```

`watch` 的退出码：
- `10`：有待批请求。按第 6 步审核，用 `decide` 批复后，再次 `watch`。
- `11`：仍在运行，暂时没有请求。再次 `watch`。
- `0`：任务成功结束，打印摘要。
- `1`：任务结束但出错，或需要确认，打印摘要。

`read`、`auto`、`full` 档或加了 `--no-approval` 时没有审批，短任务可以前台运行；长任务放后台，期间可以用 `progress` 查看进度。

其他子命令：`show [运行名|last]`、`list`（显示待批、待确认、额度用尽等状态）、`resume`（见第 6、7 步）。

### 5. 读结果并核实

摘要包含：状态、工作目录、档位、实际模型、thread_id、耗时、token 用量、额度占用、审批统计、执行过的命令（含退出码）、文件改动，以及 Codex 的最终报告。

完整记录在 `~/.codex-cli/runs/<运行名>/`，包括 `prompt.md`、`events.jsonl`（双向 JSON-RPC 全量记录）、`approvals/`、`result.md`、`summary.txt`、`stderr.log`。

`run` / `resume` 的退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 出错 |
| 2 | 参数错误或嵌套 |
| 3 | 超时（可续跑） |
| 4 | CLI 或登录问题 |
| 5 | 额度用尽或被限流（可续跑） |
| 6 | 需要用户确认 |

核实改动后，用中文向用户汇报：做了什么、改了哪些文件、核实结果、未完成项；主会话替用户批准或回答过的关键事项也要列出。

### 6. 审批：主会话审核 Codex 的请求

`watch` 列出的请求有五类：**命令审批**（附命令、目录、理由，以及需要联网时的网络信息）、**文件修改审批**（附 diff）、**额外权限审批**、**提问**（request_user_input，附问题 id 和选项）、**MCP 请求**。

审核依据两条：一是用户对本任务的授权范围（用户原话加任务简报），二是主会话自己的行为规则——凡是主会话自己去做也要先问用户的事，替 Codex 批准前同样先问用户。

**主会话可以自行批准**（`decide <运行名> <编号> allow`）：
- 任务范围内、只作用于工作目录（或 `--add-dir`）的操作；
- 只读或可撤销的操作：构建、测试、lint、格式化；
- 删除或覆盖 Codex 本次自己生成的文件；
- 任务需要的项目内依赖安装、读取公开网页和文档（联网提权）。
- 同类安全请求会反复出现时，可以加 `--always`（对应 Codex 的 acceptForSession），本线程内同类请求自动通过。

**主会话可以自行拒绝**（`decide ... deny -m "原因 + 可行的替代做法"`，说明会作为消息发给 Codex）：
- 超出任务范围的操作，或作用于工作目录以外；
- 读取凭据或密钥，或把数据外发到来历不明的地址；
- 绕过之前被拒绝的操作。
- 需要立刻停止本轮时，加 `--cancel`。

**必须先用 `AskUserQuestion` 问用户**，再 `decide ... --by user`：
- 删除或覆盖用户已有的文件，`git reset --hard` / `git clean` / 强制操作，批量修改数据；
- 推送、发布、部署、发消息、创建 PR，或其他会产生费用的操作；
- sudo、修改系统或全局配置；
- `git commit`、打 tag、删除分支（除非用户在任务里明确要求过）；
- 主会话拿不准的请求。

**提问**（request_user_input）：
- 答案能从用户的原始要求、对话上下文、项目约定或稳妥默认值推出来的，主会话自己回答，并在汇报里说明替用户选了什么：

  ```bash
  python3 $X decide <运行名> <编号> answer --answers '{"<问题 id 或问题原文>": "<选项 label 或自由文本>"}'
  ```

- 涉及用户的偏好、取舍或成本的，用 `AskUserQuestion` 问用户：选项照抄，推荐项放第一个并标注「(推荐)」，问题里写清楚是哪个委派任务。拿到答复后再 `decide ... answer --by user`。

**处理节奏**：
- 等待批复期间，整体超时 `--timeout` 暂停计时。及时批复，Codex 一直在等。超过 `--approval-timeout`（默认 30 分钟）没批复就按拒绝处理，问用户时要提醒这一点。
- 逐个审核请求。`all` 只能配合 `deny` 使用。
- 不得替用户回答本该由用户回答的问题，也不得自行放宽本该由用户放宽的权限。

**兜底：退出码 6（需要用户确认）**。Codex 只在报告结尾用文字提问，或者用约定的 `<needs-confirmation>` 块提问时，摘要里会出现「待确认」段。处理方式同上：主会话能答就答，答不了就问用户。然后在同一线程上续跑（同样放后台、循环 `watch`）：

```bash
python3 $X resume <运行名> --id <新运行名> --answer "……"
```

### 7. 中断续跑（额度用尽 / 超时）

Codex 的线程会自动保存，`resume` 用 `thread/resume` 在**同一个 thread_id** 上继续，并沿用原来的工作目录、模型、强度、档位和配置。已实测：续跑时 thread_id 不变，Codex 能记得之前的上下文。

遇到额度用尽（`usageLimitExceeded` / `rateLimitExceeded`）时，脚本会从 `account/rateLimits/updated` 里取出重置时间，在摘要末尾给出一次性 cron 表达式和续跑命令。

**主会话默认这样处理**（用户说不要时除外）：
1. 告诉用户：任务中断了，额度在什么时候重置，到时会自动续跑。
2. 用 `CronCreate` 安排一次性定时（`recurring: false`，cron 表达式用摘要里给的那个）。prompt 写成能独立看懂的指令，例如：
   > Codex 额度已重置。用 codex-cli 续跑中断的委派任务：后台运行 `python3 ~/.claude/skills/cli-manager/scripts/cli_delegate.py resume <运行名> --id <新运行名>`，按技能第 6 步循环 watch 审批直到结束，核实后向用户汇报；如果又遇到额度用尽，按新的重置时间再定时一次。

   把定时任务的 ID 和实际触发时间告诉用户。
3. 触发后，先确认用户在这段时间没有改动相关文件，再续跑。
4. 最多连续定时 3 次；还不行就问用户。如果重置时间在 12 小时以后（比如周额度），先问用户要不要等。

**备选做法**：后台运行 `... resume <运行名> --wait`，脚本会一直等到额度重置后再跑。

**注意**：定时任务和后台等待都只在当前主会话存活期间有效，这一点要告诉用户。

### 8. 追加任务

在同一线程上继续追加任务：`resume <运行名> --id <新运行名> -p "接着做：……"`。可以用 `-m`、`-e`、`--timeout`、`--access` 覆盖原来的参数；改 `--access` 必须在用户授权范围内。

## 安装与配置

- 安装 CLI：`npm i -g @openai/codex`，再把 `codex` 链接到 PATH 里的某个目录（例如 `~/.local/bin`），然后执行 `codex login`。
- CLI 路径默认按以下顺序查找：`CODEXCLI_BIN` → PATH 里的 `codex` → `~/.local/bin/codex` → ChatGPT.app 内置的 codex。运行记录目录可以用 `CODEXCLI_RUNS` 修改，账号目录可以用 `CODEXCLI_HOME` 或 `--codex-home` 指定。
