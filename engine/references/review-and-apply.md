# 审核与应用

命令中的 `$R` 为 `scripts/delegate.py` 的绝对路径。主会话按任务授权审核、验证并处理成果。

## 工作目录与基线

- 提交 Git 任务时，冻结当时的 `HEAD`。只有明确需要未提交文件时才用 `--include`，每项为 Git 根相对文件路径。工作树放在尝试自己的独立 Git 仓库下。提交目录位于 Git 仓库子目录时，工作目录映射到任务工作树中的对应子目录。项目路径与调度状态目录不能重叠。
- 非 Git 项目建立隔离副本，排除常见依赖、缓存、工具配置和凭据路径。非 Git 副本上限 300 MiB，复制时单个普通文件上限 64 MiB。基线和应用限制见源码。Git 基线中的子模块和受保护路径会被拒绝。快照读写拒绝硬链接和越界符号链接。
- 上游任务审核通过后，下游按依赖拓扑顺序叠加已批准补丁，并记录版本。变更已完整存在于基线时跳过该补丁。上游退回或重新生成集成稿会使受影响的下游尝试过期。最终应用要求报告中所有上游变更已应用，版本一致。
- Codex 开放档会把原项目中被忽略的内容克隆进工作树，并允许子代理读写 `--write-dir`。这些路径以及子代理在工作树里留下的被忽略产物（截图、日志、`quick*.json`）不进任务快照、检查点、补丁和 `task apply`。需要交付的文件应放在受版本控制的路径，或由主会话从 `--write-dir` 自行取用。
- `--kind review` 以只读任务环境执行，结束时若检查到工作树变化，任务记为失败。代码任务也在独立工作树中运行，结果以实际差异为准。
- 任务工作树所在卷把两种拼写解析为同一路径时，仅改变大小写或 Unicode 规范化形式的改名会阻止生成可批准的检查点。

## 审核报告与运行验证

```bash
python3 "$R" task status patch
python3 "$R" task review approve patch --revision "$REPORT_REVISION"
```

1. `task status` 返回尝试和 `report_path`、`summary_path`、`patch_path`。核对报告中子代理自述、变更路径、检查点提交、依赖版本、验证结果及实际补丁。报告的 `capabilities` 是这次尝试实际生效的档位、网络和读写目录，`approvals` 按顺序列出本次尝试所有运行（含续跑前）的每条越界请求：命令、路径、网络目标、结果，以及由谁决定（`planner` 主会话、`floor` 安全底线、`repeat` 重复请求、`auto` full 档自动批准、`timeout` 到期）。代码任务里用自带生图产生的图片列在 `artifacts`（`kind` 为 `generated_image`），`path` 是控制器保存的副本，`copies` 是子代理放在工作树改动路径或 `--write-dir` 里的同内容文件。取当前报告文件的完整 `revision` 作为审核参数。
2. `approve` 验证报告和补丁摘要，再运行提交时指定的 `--verify`。验证用临时副本和本机 Codex 沙箱，与任务的能力档位无关：它始终禁网，副本里没有被忽略的内容，依赖 `node_modules` 的检查应由子代理在任务中运行并写入报告。控制器优先使用已登记的第一个 Codex 实例所配置的 CLI，否则从 `PATH` 查找。验证环境使用临时主目录。返回结果的 `approved` 才是应用或生图导出所用的摘要。即使未配置验证，报告也会加入 `not_configured` 后生成审核版本。
3. 验证失败时查看 `review_candidate` 和最新报告的 `verification`，任务不会进入已批准状态。命令退出成功只表示这次审核操作返回了结果，仍需检查 `state` 与 `approved`。缺少有效结构化报告时先补齐。选择退回修改时，指定当前报告版本和 `--note`，再用 [续跑或重试](handoff.md#续跑重试和等待额度) 继续。

```bash
python3 "$R" task review request-changes patch --revision "$CURRENT_REVISION" --note '补充测试并核对边界条件'
```

## 预览、应用和恢复

```bash
python3 "$R" task apply patch --revision "$APPROVED_REVISION" --check
python3 "$R" task apply patch --revision "$APPROVED_REVISION" --apply
python3 "$R" task apply patch --recover
```

- `--check` 返回 `ready` 或 `blocked`，冲突时列出受影响路径。代码任务的 `--apply` 以已批准版本重新核对项目目录、受影响文件和 Git 暂存状态。有漂移时停止。应用把差异写入原工作目录，保留原 Git 索引，提交由主会话按授权处理。
- Git 文件按对应版本的 `.gitattributes` 和原仓库本地行尾设置生成工作目录字节。非 Git 文件按原字节处理。
- 应用文件与目录互换时，待替换目录中的额外内容会阻止操作。预览、应用和恢复会核对原路径的精确拼写。新增符号链接的目标也会在原项目中校验。
- 应用前保存变更路径的前镜像和操作日志。预览会检查同卷暂存位置并返回 `staging_parent`。独立卷根目录上的 Git 项目可使用同卷的 Git 管理目录暂存，避免 `git add -A` 收集原文件。非 Git 项目可使用项目根内的私有目录，任务快照会排除它。单个任务涉及项目内的其他挂载卷时，预览会阻止应用。
- 应用和恢复会在写入或删除前再次核对文件身份。同类型内容替换保留权限、ACL 和可复制的扩展属性，有意修改的可执行位按批准成果应用。删除或替换的原文件原子移入暂存目录，应用成功后可在 `retained` 查看。中断后用 `--recover` 处理未结算日志。若返回冲突，保留用户当前内容并逐一检查。项目锁协调本调度器进程。
- 非 Git 项目也按路径核对内容与类型。遇到漂移时保留成果并报告冲突。以下集成工作树命令只适用于 Git。

## Git 冲突的集成工作树

```bash
python3 "$R" task apply patch --revision "$APPROVED_REVISION" --integrate
python3 "$R" task apply patch --revision "$APPROVED_REVISION" --record-integration
```

`--integrate` 返回集成工作树的 `work` 路径和 `resolved_automatically`。在该工作树核对三方应用，有冲突时手动解决并在那里暂存冲突文件。用同一个已批准摘要运行 `--record-integration`，生成新的待审核版本。随后读取新报告，重新审核、验证、预览和应用。原目录暂存区继续保留。

## 清理

```bash
python3 "$R" task cleanup patch
python3 "$R" task cleanup patch --confirm
```

先查看 `eligible`、`paths` 和 `dependencies`。任务已结束、没有活动尝试且无其他未结束任务依赖时，可确认清理。清理回收工作目录、基线、集成目录或参考图等列出的资源，保留报告、补丁和审计记录。应用后保留的原文件会列在 `application_staging`。清理核对文件身份、内容、权限、ACL 和扩展属性。恢复也会识别已发布文件的权限或属性变化，发现冲突时保留文件。

源码对应 [报告、审核与应用命令](../scripts/delegate.py)、[验证与审核](../scripts/cli_manager/controller.py) 和 [基线、冲突检测、回滚](../scripts/cli_manager/workspace.py)。
