# cli-manager 原生协议验证记录

## 本机已验证

- macOS 上 Claude Code 2.1.329 与 Codex 0.171.0 的版本和帮助。两个空 `CLAUDE_CONFIG_DIR` 均返回匹配的目录和未登录状态。两个空 `CODEX_HOME` 均返回未登录。
- 本机 Codex App Server 的 `initialize`、`account/read`、`model/list` 及协议 schema。免登录临时线程确认启用了名为 `delegate` 的权限配置，使用隔离工作目录，网络关闭。
- 本机 Codex 沙箱夹具中任务目录可写，外部测试文件与凭据标记不可读，原项目测试文件不可写。
- 模拟 CLI 子进程从公开命令验证 Git 和非 Git 派工、结构化结果、主会话应答、取消、跨后端交接、审核应用及图像交付。

## 实际订阅仍需验收

1. 在两个独立实例分别登录。切换或退出一个实例后，检查另一个实例和桌面登录仍有效。
2. 分别运行只读和写入任务。准备外部标记文件，检查原工作目录、其他实例、账户目录、工具提权、文件编辑和后台子进程边界。Claude 的限制模式与强制 Bash 沙箱须在真实模型工具调用中验收。
3. 捕获脱敏的报告、用户提问、正常中断、恢复和额度样本。Claude 文本窗口若无法可靠解析恢复时间，保留原文本及未知时间。Codex 采用服务返回的窗口和百分比。
4. 用一张参考图完成生图，在同实例同时运行只读任务，核对线程、模型、文件哈希、额度和交付产物。
5. 在预期发布的 CLI 版本、平台和组织设置上重复验证。版本不匹配或权限探针失败时保持任务关闭。

官方接口参考 [Claude 非交互模式](https://code.claude.com/docs/en/headless)、[Claude 沙箱](https://code.claude.com/docs/en/sandboxing)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Codex 权限](https://learn.chatgpt.com/docs/permissions)。
