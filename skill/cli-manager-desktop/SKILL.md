---
name: cli-manager-desktop
description: 查询本机 CLI Manager 的 Codex 与 Claude Code 实例、额度和队列，或按用户授权使用已登记的实例派工、监控和审核。
---

# CLI Manager

用此 Skill 目录内的 `scripts/manager` 访问随 macOS 应用安装的 CLI。桌面应用和 CLI 使用同一个实例与任务库。

- 查状态时先运行 `scripts/manager desktop snapshot`。它返回有版本的 JSON，并标注登录、任务活动、额度和观测时间。需要主动查询某个提供方时，用 `scripts/manager instance status <实例>`。
- 由掌握完整简报的规划会话运行 `scripts/manager planner open`，私下保留返回的 `receipt` 路径。用 `scripts/manager planner --receipt "$RECEIPT" task submit --instance <实例> …` 派工，再运行 `scripts/manager queue work`。项目、任务和实例均须在用户授权范围内。凭证留在规划会话，不写入工作区、子任务、工具输出摘要或交接简报。
- 规划会话用 `scripts/manager planner --receipt "$RECEIPT" task watch <任务> --since <游标>` 查看问题和权限请求。核对任务范围与用户授权后，使用 `planner --receipt "$RECEIPT" --request-ref "$REF" task decide <任务> <请求ID> answer|allow|deny` 处理。回答时按 CLI 的 `--answers` 格式提供内容。凭证代表决策归属，权限检查仍由原 CLI 执行。拒绝越界、扩大沙箱或需额外授权的操作。
- 让受管请求留在原规划会话，恢复任务时重新读取当前请求引用。规划会话暂时离线时保留原归属，由同一个规划会话用原凭证继续。归属未知的请求不会转到桌面用户。用 `planner --receipt "$RECEIPT" close` 显式撤销不再使用的凭证。
- 登录、退出和切换交给用户在桌面应用或交互式终端完成。汇报时使用实例名、任务 ID 和公开状态。删除先运行 `instance delete <实例>` 查看影响，得到用户授权后再使用 `--confirm`。
- 通过 `scripts/manager docs` 获取已安装参考目录。根据任务阅读 `scheduling.md`、`handoff.md` 或 `review-and-apply.md`。审核报告和实际补丁后再应用任务结果。
