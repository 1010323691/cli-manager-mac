---
name: cli-manager
description: 管理独立登录的 Claude Code 和 Codex CLI 实例。用户要求通过 CLI 子代理派工、多账户调度、跨 CLI 交接、审核或应用任务成果，或在 CLI 工作流中明确要求单张或批量生图时使用。
---

# cli-manager

## 触发条件

用户点名受管 CLI 实例，或要求建立实例、按额度并行派工、接手续跑、审核交付时，使用本 Skill。在这类工作流中，用户明确要求生图时使用受管 Codex CLI。

## 主流程

1. 用 Python 3.11+ 调用本 Skill 的 `scripts/delegate.py`。先确认 [实例和登录状态](references/instances.md)，按用户授权限定任务可用的实例。
2. 主会话写简报，列清目标、范围、验证要求和依赖。用 `task submit` 入队，后台启动 `queue work`，通过 `task watch` 和 `task decide` 处理进度及待批请求。参数和额度判断见 [任务与调度](references/scheduling.md)。
3. 任务中断时，按任务状态和用户授权决定等待、原会话续跑、重试或交接。由主会话整理交接简报。见 [续跑与交接](references/handoff.md)。
4. 主会话读取报告和实际补丁，审核结果后按依赖顺序预览并应用。生图按同一队列跟踪，审核后导出。见 [审核与应用](references/review-and-apply.md) 和 [生图](references/images.md)。
