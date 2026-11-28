# cli-manager

在本机管理独立登录的 Claude Code 与 Codex CLI 实例，由主会话派工、监控、审核和交付。

从 [SKILL.md](SKILL.md) 了解何时使用与主流程。执行入口位于 `scripts/delegate.py`，以下参考文档中的 `$R` 指该脚本的绝对路径。用 Python 3.11+ 运行。

```bash
R=/绝对路径/cli-manager/scripts/delegate.py
python3 "$R" --help
```

| 事项 | 参考文档 |
|---|---|
| 安装、账户、实例状态和本地数据 | [实例管理](references/instances.md) |
| 提交任务、查额度、并发、监控和审批 | [任务与额度调度](references/scheduling.md) |
| 等待额度、原会话续跑、重试和跨实例交接 | [续跑与交接](references/handoff.md) |
| 工作树、报告、验证、应用和清理 | [审核与应用](references/review-and-apply.md) |
| 单张、批量、参考图、导出和总览图 | [生图](references/images.md) |

## 验收与旧版资料

在 Skill 目录执行 `python3 -B -m unittest discover -s tests -v`。测试使用模拟 CLI、临时项目和临时状态库。实际订阅的账户切换、工具审批、额度事件和生图验收见 [原生协议验证记录](references/v2-validation.md)。

从旧任务入口切换前，先结束旧任务或人工保存其工作树、补丁和报告。用当前入口创建实例并重新登录，审核后的补丁与简报可用于重新提交。Skill 从 `cli-delegate` 改名为 `cli-manager` 时沿用了原 v2 状态目录，已有 v2 实例和任务可以直接查看。暂停派发时保留状态库和尝试目录。

旧任务入口的参数与操作见 [Claude 旧版手册](backends/claude/GUIDE.md)、[Codex 旧版手册](backends/codex/GUIDE.md) 和 [旧版生图手册](backends/codex/IMAGE.md)。
