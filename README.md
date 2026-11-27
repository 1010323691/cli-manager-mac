# CLI Manager for macOS

用屏幕顶部的小浮层查看 CLI 工作状态，在原生窗口中管理 Codex 和 Claude Code 实例。应用与 CLI Manager 共用实例和任务状态库。

## 安装给 Codex 和 Claude Code

请在实际要使用应用的 Mac 上安装。需要 macOS 14 或更新版本、含 macOS 26 SDK 的 Xcode、Git 和 Python 3.11+。运行实例还需要对应提供方的 CLI。应用在 macOS 26+ 使用 Liquid Glass，较早系统使用原生半透明材质。

### 1. 取得源码与固定版本的引擎

macOS 项目的公开源代码在 `1010323691/cli-manager-mac`。打包和 Python 集成测试还需要原 CLI Manager 的完整源码，当前位于私有的 `1010323691/MySkills` 仓库。请使用已有的 GitHub 访问权限。缺少权限时，报告缺少哪一个 checkout，由用户提供访问权限或固定版本的本地路径。

如果 Mac 已有两个 checkout，进入 macOS 项目的根目录并将 `CLI_MANAGER_LEGACY_SOURCE` 指向旧仓库里的 `cli-manager/` 子目录。打包脚本核对该目录的相关版本，并从 Git 的固定提交取文件。需要固定的提交可从 [engine-revision](resources/engine-revision) 读取。已有的 CLI 源码也可以放在本项目旁边，目录名用 `cli-manager`。

首次使用可在一个新的目录中克隆两份源码。以下命令应在有权读取两个仓库时运行：

~~~sh
git clone https://github.com/1010323691/cli-manager-mac.git
git clone --no-checkout https://github.com/1010323691/MySkills.git cli-manager-engine
cd cli-manager-mac
engine_checkout="$(cd ../cli-manager-engine && pwd -P)"
engine_revision="$(cat resources/engine-revision)"
git -C "$engine_checkout" checkout --detach "$engine_revision"
export CLI_MANAGER_LEGACY_SOURCE="$engine_checkout/cli-manager"
~~~

### 2. 安装应用、完整 Skill 和桌面配套 Skill

~~~sh
cli_manager_skill_store="$HOME/.local/share/cli-manager/skills"
./scripts/install \
  --apps-dir /Applications \
  --skills-dir "$cli_manager_skill_store" \
  --with-cli-manager-skill \
  --link-skills-dir "$HOME/.codex/skills" \
  --link-skills-dir "$HOME/.claude/skills"
~~~

- 应用安装到 `/Applications/CLI Manager.app`。安装器构建并验证本地签名。
- `cli-manager` 是完整的原版 Skill，包含 `SKILL.md`、CLI 脚本、提供方适配及参考资料。`cli-manager-desktop` 是配套 Skill，提供桌面快照与规划会话的决策归属入口。已安装的配套 Skill 通过 `app-path` 定位应用。
- 两份 Skill 都集中存放在 `$HOME/.local/share/cli-manager/skills`，并分别软链接到 Codex 和 Claude Code 的 `skills` 目录。使用自定义 Skills 路径时修改对应的 `--link-skills-dir`。
- 安装器对每个目标先暂存再发布。应用、Skill 目录或软链接已存在时会退出。已有安装时先核对它的版本、链接目标和运行中的会话，再由用户决定如何更新。安装到 `/Applications` 需要当前执行用户对该目录有写入权限。

### 3. 核验并让智能体加载 Skill

~~~sh
app="/Applications/CLI Manager.app"
codesign --verify --strict "$app"
/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist"

for agent_skills in "$HOME/.codex/skills" "$HOME/.claude/skills"; do
  for skill in cli-manager cli-manager-desktop; do
    test -L "$agent_skills/$skill" &&
      test "$agent_skills/$skill" -ef "$HOME/.local/share/cli-manager/skills/$skill" &&
      test -r "$agent_skills/$skill/SKILL.md" &&
      readlink "$agent_skills/$skill" || exit 1
  done
  test -r "$agent_skills/cli-manager/scripts/delegate.py" || exit 1
done
open -a "$app"
~~~

在 Codex 和 Claude Code 各开一个新会话，确认可读取 `cli-manager` 与 `cli-manager-desktop`。先用当前智能体目录里的配套脚本读取只读快照：

~~~sh
"$HOME/.codex/skills/cli-manager-desktop/scripts/manager" desktop snapshot
# Claude Code 使用 "$HOME/.claude/skills/cli-manager-desktop/scripts/manager"
~~~

派工前阅读完整 Skill 的调度参考，并使用下文带规划会话凭证的 `manager planner`。完整 `cli-manager` 仍保留可独立使用的原 CLI 入口。直接调用其中的 `scripts/delegate.py` 会按原引擎的规则访问状态库，不经过桌面桥接的版本守卫或规划者归属检查。提供方登录由用户在原生交互流程中完成。

## 仅构建或启动

在本仓库根目录准备好同一个固定版本的引擎后运行：

~~~sh
./scripts/build-app
open ".build/CLI Manager.app"
~~~

双击应用会打开终端工作区。选中实例后点「启动终端」才会启动 CLI。仅需菜单栏时，可用 `open -n -a "/Applications/CLI Manager.app" --args --menu-bar` 启动。菜单栏的波形图标可打开实例工作区、固定当前屏幕的摘要或开关顶边入口。鼠标在任一显示器顶部中央停留约四分之一秒，白色毛玻璃摘要向下滑出。移开后收起，固定按钮可保留摘要。摘要列出最多三个已核实的启动中、运行中或停止中的托管任务。系统隐藏菜单栏时，应用让出屏幕顶部。在管理窗口按 ⇧⌘P 可临时显示可用键盘操作的摘要，按 Esc 收起。单次显示会保留悬停开关的原设置。

## 状态与账户

- 应用启动后查询可用账户的额度，共用额度的实例只查询一次。托管调用开始及结束时查询对应实例。内嵌用户终端先在实例锁内查询再启动，退出时释放锁后再查。
- 每八秒读取本地状态并检查是否有新的调用事件、已到点的重置或待查询的账户。不满 100% 的窗口保存提供方报告的重置时间。额度查询未返回新读数时，重置重试逐步退避。一次轮询间隔内已经结束的短调用会合并为退出后的查询。读取本地状态只加载本机快照。
- 侧栏选择决定主窗口显示哪个实例的终端。顶边摘要显示有活动的托管任务。为任务选路时，智能体仍通过 `task submit --instance <实例>` 指定用户授权的实例。提供方查询不能区分主动外部退出与认证过期时，曾绑定账户的实例显示「登录失效」。
- 登录和切换在 Terminal.app 中进行，由提供方的真实 CLI 打开授权流程。应用只从临时回执读取退出码。账户代次改变后，之前绑定到旧代次的任务需重新指定。运行中的实例及仍被任务或账户引用的实例按 CLI 的规则保护。
- 默认使用当前 CLI 的 `~/.cli-delegate-v2`。可用 `CLI_MANAGER_STATE` 指定隔离目录，或用 `CLI_MANAGER_PYTHON` 指定 Python 的绝对路径。默认搜索 `~/.local/bin`、Homebrew 和系统 PATH。应用源码在 `Sources/`，Python 适配层在 `bridge/`。打包时复制指定 CLI 源码中的运行时和参考文档，应用和桌面配套 Skill 使用同一个包内 CLI。打包固定引擎源于 Git 提交 `8bc75540d8fd742a3fe48766afba7d676f6d37b3`，定义见 `resources/engine-revision`。包内引擎检查现有状态库的结构和版本，遇到不匹配时停止账户、任务和实例写入。升级引擎后需要更新锁定版本、核对状态库契约并重新打包。

## 委托任务的决策归属

掌握完整简报的规划会话调用已安装 Skill 的 `scripts/manager`。运行 `planner open` 后，把返回的 `receipt` 路径保存在此会话的私有上下文中，并保留到会话需要恢复时。它是授权访问规划任务的本地凭证，仅限具备本地可信权限的规划会话使用。

~~~sh
"$M" planner open
"$M" planner --receipt "$RECEIPT" task submit --id review-1 --project "$PROJECT" \
    --kind review --instance "$INSTANCE" --brief "$BRIEF"
"$M" queue work --once
"$M" planner --receipt "$RECEIPT" task watch review-1 --since 0
"$M" planner --receipt "$RECEIPT" --request-ref "$REF" task decide review-1 "$REQUEST_ID" \
    answer --answers '{"question_id":"答案"}'
~~~

`$M` 指已安装 Skill 中 `scripts/manager` 的绝对路径。`watch` 返回的 `request_ref` 绑定当前执行，续跑后须重新读取，旧引用不能处理新请求。权限请求仍可用 `allow` 或 `deny`，并由原 CLI 校验。普通桌面轮询、顶栏和菜单栏不分发委托请求。

规划会话离线时保留原归属，请求遵循提供方已有的等待与超时逻辑。`planner --receipt "$RECEIPT" close` 撤销凭证且不转移任务。旧任务或普通 `task submit` 创建的任务缺少可验证的规划者归属，新版 Skill 入口不会答复它们。原 CLI 安装保留其独立管理权限。归属守卫保护新的 Skill 与桌面入口，旧 CLI 或具备同一 macOS 用户完整状态目录权限的程序仍位于本地信任边界内。

## 用户终端与刷新

在侧栏选择实例可查看其用户终端工作区。点「启动终端」，首次启动时选择工作目录。之后可再次使用栏中明确显示的上次有效目录，也可先改选目录。SwiftTerm 的原生 PTY 嵌在主窗口内，每个实例的会话独立运行。切换实例、刷新状态或关闭主窗口会保留会话。结束按钮需确认，进程退出后工作区恢复「启动终端」，并记住已选目录。应用在每次启动前核对实例登录代次及提供方实际账户。Codex 使用该实例的 `CODEX_HOME`、文件凭据配置和原生 `on-request` 提示，Claude Code 使用同一实例的 `CLAUDE_CONFIG_DIR` 及默认交互权限模式。键盘输入送至当前获得焦点的用户终端。实例设置位于侧栏右键菜单或工作区的省略号按钮中。两种提供方的提问和权限提示均由各自 CLI 显示，应用弹出提示尚无受支持的结构化来源。账户登录仍由 Terminal.app 承接。

实例有已核实的托管任务时，选中该实例会显示只读输出，包含智能体对外消息和操作进度。实例同时运行多个任务时可在输出栏切换。输出视图消费现有运行事件，不启动第二个 CLI 会话。审批继续由拥有任务的规划会话处理。任务结束后，工作区恢复可启动状态。

用户终端持有实例锁。会话期间可读取本地状态，提供方查询和账户操作在会话结束后恢复。CLI Manager 的调度权限与委托任务决策继续由原引擎执行。终端服务本身不读凭据文件，不保存终端输出，也不把转义指令转成审批、链接或剪贴板动作。提供方可照常维护它自己的本地历史。

「读取本地状态」只读取状态库。常驻轮询另外检查调用事件和重置时间。「查询提供方」显式调用当前实例的实际 CLI，可用于重试。临时结果显示在可关闭的提示中。最近尝试与查询诊断一直显示在账户操作区。实例侧栏单独显示提供方实际报告的短期与长期额度窗口。查询失败时显示最近尝试与仍可核实的上次登录确认，历史百分比不作为当前额度。归属一致且仍在有效窗口内的确认限额保留，只有已知重置时间才显示重置时刻。调度器继续拒绝未确认登录的实例。

正在使用正式构建时，可并行构建一个新的预览目录。预览输出路径必须是尚不存在的绝对 `.app` 路径。

~~~sh
CLI_MANAGER_APP_OUTPUT="$PWD/.build/preview/CLI Manager Preview.app" \
CLI_MANAGER_BUNDLE_ID=com.myskills.cli-manager-macos.preview ./scripts/build-app
~~~

SwiftTerm 固定于 MIT 许可的 v1.17.0，许可证随应用打包。当前使用 CPU 渲染，构建不需要单独安装 Xcode Metal 组件。

本机故障的对照实验、启动链路及缓存验证见 [状态查询审计](docs/status-probe-audit.md)。

## 验证

~~~sh
python3 -B -m unittest discover -s tests -v
swift test --build-system native --scratch-path .build/native-terminal-check
codesign --verify --strict ".build/CLI Manager.app"
~~~

Python 集成测试从同一个 Git 固定提交导出引擎到临时目录。测试通过隔离状态库、真实 CLI 命令入口和本地模拟提供方覆盖交互式认证、已有记录守卫、额度时效、队列和删除条件。UI 布局参考了 [Apple 的材质指南](https://developer.apple.com/design/human-interface-guidelines/materials)、[菜单栏指南](https://developer.apple.com/design/human-interface-guidelines/the-menu-bar) 和 [Boring Notch 的悬停交互](https://github.com/TheBoredTeam/boring.notch)。
