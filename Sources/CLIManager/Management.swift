import AppKit
import SwiftUI

@MainActor
struct Management: View {
    @ObservedObject var model: Dashboard
    @Binding var edgeEnabled: Bool
    @Environment(\.colorScheme) private var scheme
    @State private var route: Dialog?

    init(model: Dashboard, edgeEnabled: Binding<Bool>, settingsOnOpen: InstanceRecord? = nil) {
        self.model = model
        _edgeEnabled = edgeEnabled
        _route = State(initialValue: settingsOnOpen.map(Dialog.settings))
    }

    private enum Dialog: Identifiable {
        case create
        case settings(InstanceRecord)
        case stop(UserTerminalSession)
        case delete(InstanceRecord, DeletionPreview)
        case account(InstanceRecord, String)

        nonisolated var id: String {
            switch self {
            case .create: "create"
            case let .settings(instance): "settings/\(instance.terminalKey)"
            case let .stop(session): "stop/\(session.id)"
            case let .delete(instance, _): "delete/\(instance.id)"
            case let .account(instance, operation): "\(operation)/\(instance.id)"
            }
        }
    }

    private var settingsPresented: Bool {
        if case .settings = route { return true }
        return false
    }

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 223)
            Rectangle().fill(WorkspacePalette.color(WorkspacePalette.rule)).frame(width: 1)
            VStack(spacing: 0) {
                if let instance = model.selected {
                    terminalWorkspace(instance)
                } else {
                    toolbar
                    EmptyState(title: model.snapshot == nil ? "正在读取本地状态" : "还没有实例",
                               text: "从左侧添加 Codex 或 Claude Code 实例。", symbol: "rectangle.stack")
                    footer
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(WorkspacePalette.color(WorkspacePalette.paper))
        }
        .frame(minWidth: 780, idealWidth: 920, minHeight: 540, idealHeight: 680)
        .coordinateSpace(name: "management")
        .overlay(alignment: .bottomTrailing) {
            if !settingsPresented, let notice = model.notice {
                toast(notice).padding(.bottom, 15).padding(.trailing, 20)
            }
        }
        .task(id: model.notice) {
            guard let notice = model.notice else { return }
            try? await Task.sleep(for: .seconds(5))
            if !Task.isCancelled, model.notice == notice { model.clearNotice() }
        }
        .sheet(item: $route) { value in
            switch value {
            case .create: CreateSheet(model: model)
            case let .settings(record):
                LiveInstanceSettings(model: model, key: record.terminalKey, content: settingsSheet)
            case let .stop(session): stopSheet(session)
            case let .delete(instance, preview): deleteSheet(instance, preview)
            case let .account(instance, operation): accountSheet(instance, operation)
            }
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(spacing: 10) {
                Image(systemName: "waveform.path")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(WorkspacePalette.color(WorkspacePalette.accent))
                    .frame(width: 34, height: 34)
                    .background(WorkspacePalette.color(WorkspacePalette.selection), in: RoundedRectangle(cornerRadius: 10))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("CLI Manager").font(.system(size: 17, weight: .semibold))
                    Text("本地 CLI 实例").font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 5)
            Rectangle().fill(WorkspacePalette.color(WorkspacePalette.rule))
                .frame(height: 1).padding(.horizontal, 14)
            HStack {
                Text("实例").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button { model.clearNotice(); route = .create } label: {
                    Image(systemName: "plus.circle.fill").font(.system(size: 16, weight: .medium))
                }
                .buttonStyle(.plain)
                .help("新建实例")
                .accessibilityLabel("新建实例")
                .keyboardShortcut("n", modifiers: .command)
            }
            .padding(.horizontal, 16)
            ScrollView {
                LazyVStack(spacing: 3) {
                    ForEach(model.instances) { instance in
                        instanceRow(instance)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 2)
            }
            Toggle(isOn: $edgeEnabled) { Label("屏幕顶边摘要", systemImage: "rectangle.topthird.inset.filled") }
                .toggleStyle(.checkbox)
                .font(.caption)
                .padding(.horizontal, 16)
                .padding(.bottom, 15)
        }
        .padding(.top, 10)
        .foregroundStyle(WorkspacePalette.color(WorkspacePalette.ink))
        .background(WorkspacePalette.color(WorkspacePalette.sidebar))
    }

    private func instanceRow(_ instance: InstanceRecord) -> some View {
        let selected = model.selection == instance.id
        let slots = QuotaReading.split(instance.quota.displayWindows).slots.map { quotaReading($0, instance) }
        let querying = model.pending[instance.id] == Dashboard.providerQueryProgress
        let failed = instance.probeFailed || model.probeFailure(instance) != nil
        let activity: BadgeData
        if model.snapshotError != nil {
            activity = BadgeData("本地状态待核实", "questionmark.circle", .secondary)
        } else if model.probeFailure(instance) != nil, instance.activity.title == "空闲" {
            activity = BadgeData("查询失败", "exclamationmark.triangle", .orange)
        } else {
            activity = instance.activity
        }
        let secondary = querying ? "查询中" : failed && activity.title != "查询失败" ? "查询失败" : nil
        let reset = model.snapshotError == nil ? instance.quota.limitResetAt : nil
        let activeTerminal = model.terminalSession(for: instance)?.phase.isActive == true
        let help = ([instance.id, activity.title, secondary, activeTerminal ? "有用户终端" : nil,
                     reset.map { "额度重置 \(timestamp($0))" }]
                    .compactMap { $0 } + slots.map(\.explanation)).joined(separator: "；")
        return Button {
            model.choose(instance.id)
            model.terminalSession(for: instance)?.focusIfMounted()
        } label: {
            VStack(spacing: 4) {
                HStack(spacing: 7) {
                    ProviderTile(provider: instance.provider, size: 25)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(instance.id).font(.subheadline.weight(.medium)).lineLimit(1)
                        HStack(spacing: 4) {
                            Label(activity.title, systemImage: activity.icon)
                                .foregroundStyle(legible(activity.color, in: scheme))
                                .fixedSize(horizontal: true, vertical: false)
                            if let secondary {
                                Text(secondary).foregroundStyle(legible(querying ? .blue : .orange, in: scheme))
                            } else if let reset, activity.title == "额度耗尽" {
                                ViewThatFits(in: .horizontal) {
                                    Text(Date(timeIntervalSince1970: reset).formatted(.dateTime.month(.defaultDigits)
                                        .day().hour().minute())).fixedSize()
                                    Image(systemName: "clock").fixedSize()
                                }
                                .foregroundStyle(.secondary)
                            }
                        }
                        .font(.caption2).lineLimit(1)
                    }
                    Spacer(minLength: 0)
                    if activeTerminal {
                        Image(systemName: "terminal.fill").font(.system(size: 9, weight: .medium))
                            .foregroundStyle(legible(.blue, in: scheme))
                            .accessibilityHidden(true)
                    }
                    Image(systemName: "chevron.right").font(.caption2.weight(.semibold))
                        .foregroundStyle(WorkspacePalette.color(WorkspacePalette.accent)).opacity(selected ? 1 : 0)
                        .accessibilityHidden(true)
                }
                HStack(spacing: 5) {
                    ForEach(slots.indices, id: \.self) { MiniQuota(reading: slots[$0]) }
                }
            }
            .padding(.horizontal, 8)
            .frame(height: 59)
            .frame(maxWidth: .infinity)
            .background(WorkspacePalette.color(selected ? WorkspacePalette.selection : WorkspacePalette.row),
                        in: RoundedRectangle(cornerRadius: 9))
            .overlay {
                if selected {
                    RoundedRectangle(cornerRadius: 9)
                        .strokeBorder(WorkspacePalette.color(WorkspacePalette.selectionBorder))
                        .allowsHitTesting(false)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(help)
        .accessibilityIdentifier("instance-\(instance.id)")
        .contextMenu {
            Button("实例设置…") { route = .settings(instance) }
        }
        .measureFixture("instance-\(instance.id)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func quotaReading(_ window: QuotaWindow?, _ instance: InstanceRecord) -> QuotaReading {
        QuotaReading(window: window, provider: instance.provider,
                     querying: model.pending[instance.id] == Dashboard.providerQueryProgress,
                     failed: instance.probeFailed || model.probeFailure(instance) != nil,
                     snapshotUnavailable: model.snapshotError != nil)
    }

    private var toolbar: some View {
        HStack(spacing: 12) {
            Text("实例管理")
                .font(.system(size: 16, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
            Spacer()

            Button {
                Task { await model.refresh() }
            } label: {
                Label("读取本地状态", systemImage: "arrow.clockwise")
            }
            .help("重新读取本地状态")
            .keyboardShortcut("r", modifiers: .command)
        }
        .padding(.horizontal, 23)
        .frame(height: 54)
        .background(WorkspacePalette.color(WorkspacePalette.toolbar))
        .overlay(alignment: .bottom) {
            Rectangle().fill(WorkspacePalette.color(WorkspacePalette.rule)).frame(height: 1)
        }
        .measureFixture("toolbar")
    }

    private func terminalWorkspace(_ instance: InstanceRecord) -> some View {
        let session = model.terminalSession(for: instance)
        let active = session?.phase.isActive == true
        let directory = model.terminalDirectory(for: instance)
        let managed = active ? [] : model.managedAttempts(for: instance)
        return VStack(spacing: 0) {
            HStack(spacing: 10) {
                if managed.isEmpty {
                    directoryControl(instance, directory: directory, active: active)
                } else {
                    Label("托管任务", systemImage: "waveform.path").fontWeight(.medium)
                }
                Spacer(minLength: 12)
                if let session, active {
                    StateBadge(data: session.phase.badge)
                    Button {
                        route = .stop(session)
                    } label: {
                        Image(systemName: "stop.circle").frame(width: 15, height: 17)
                    }
                    .help("结束会话")
                    .accessibilityLabel("结束会话")
                    .accessibilityIdentifier("terminal-stop")
                    .disabled(session.phase == .stopping)
                } else if !managed.isEmpty {
                    StateBadge(data: BadgeData("只读", "lock", .secondary))
                } else {
                    StateBadge(data: BadgeData("未启动", "circle", .secondary))
                }
                Button {
                    Task { await model.refresh() }
                } label: {
                    Image(systemName: model.snapshotError == nil ? "arrow.clockwise" : "exclamationmark.circle")
                        .frame(width: 15, height: 17)
                        .foregroundStyle(model.snapshotError == nil ? Color.primary : legible(.orange, in: scheme))
                }
                .help(model.snapshotError.map { "读取本地状态失败 · \($0)" } ?? "读取本地状态")
                .accessibilityLabel("读取本地状态")
                .accessibilityIdentifier("terminal-reload")
                .keyboardShortcut("r", modifiers: .command)
                Button {
                    route = .settings(instance)
                } label: {
                    Image(systemName: "ellipsis").frame(width: 15, height: 17)
                }
                .help("实例设置")
                .accessibilityLabel("实例设置")
                .accessibilityIdentifier("instance-settings")
            }
            .padding(.horizontal, 13)
            .font(.caption)
            .frame(height: 46)
            .foregroundStyle(WorkspacePalette.color(WorkspacePalette.ink))
            .background(WorkspacePalette.color(WorkspacePalette.toolbar))
            .overlay(alignment: .bottom) {
                Rectangle().fill(WorkspacePalette.color(WorkspacePalette.rule)).frame(height: 1)
            }
            .measureFixture("toolbar")
            VStack(spacing: 0) {
                if let session, active {
                    DockedTerminal(session: session)
                } else if !managed.isEmpty {
                    ManagedActivityPane(model: model, attempts: managed)
                        .id(instance.terminalKey)
                } else {
                    idleTerminal(instance)
                    if let session, case let .failed(message) = session.phase {
                        Label(message, systemImage: "exclamationmark.circle")
                            .font(.caption)
                            .foregroundStyle(legible(.orange, in: scheme))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 13)
                            .frame(height: 27)
                            .background(.bar)
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .measureFixture("workspace")
        }
    }

    private func directoryControl(_ instance: InstanceRecord, directory: URL?, active: Bool) -> some View {
        let location = directory?.path
        return HStack(spacing: 7) {
            Image(systemName: "folder").accessibilityHidden(true)
            Text("工作目录").fontWeight(.medium).fixedSize()
            if active {
                Text(location ?? "—")
                    .lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(WorkspacePalette.color(WorkspacePalette.muted))
                    .help(location ?? "—")
                    .accessibilityLabel("工作目录 \(location ?? "—")")
            } else {
                Button {
                    Task { await chooseTerminalDirectory(for: instance, thenStart: false) }
                } label: {
                    Text(location ?? "选择…").lineLimit(1).truncationMode(.middle)
                }
                .help(location ?? "选择工作目录")
                .accessibilityLabel(location.map { "更改工作目录，当前 \($0)" } ?? "选择工作目录")
                .accessibilityIdentifier("terminal-directory")
            }
        }
    }

    private func idleTerminal(_ instance: InstanceRecord) -> some View {
        let blocked = model.terminalStartBlocked(instance)
        return ZStack {
            WorkspacePalette.color(WorkspacePalette.paper)
            VStack(spacing: 12) {
                Image(systemName: "terminal")
                    .font(.system(size: 23, weight: .medium))
                    .foregroundStyle(WorkspacePalette.color(WorkspacePalette.accent))
                    .frame(width: 46, height: 46)
                    .background(WorkspacePalette.color(WorkspacePalette.selection), in: RoundedRectangle(cornerRadius: 11))
                    .accessibilityHidden(true)
                Button("启动终端") { startTerminal(instance) }
                    .buttonStyle(.borderedProminent)
                    .tint(WorkspacePalette.color(WorkspacePalette.accent))
                    .disabled(blocked)
                    .accessibilityIdentifier("terminal-start")
                    .measureFixture("terminal-start")
                if blocked {
                    Button("查看实例设置") { route = .settings(instance) }.buttonStyle(.link)
                }
            }
        }
    }

    private func startTerminal(_ instance: InstanceRecord) {
        Task {
            if let directory = model.terminalDirectory(for: instance) {
                await model.openUserTerminal(instance, directory: directory)
            } else {
                await chooseTerminalDirectory(for: instance, thenStart: true)
            }
        }
    }

    private func chooseTerminalDirectory(for instance: InstanceRecord, thenStart: Bool) async {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = model.terminalDirectory(for: instance)
        panel.message = "选择「\(instance.id)」的工作目录。"
        panel.prompt = thenStart ? "在此目录启动" : "使用此目录"
        let result: NSApplication.ModalResponse
        if let window = NSApp.keyWindow { result = await panel.beginSheetModal(for: window) }
        else { result = await panel.begin() }
        guard result == .OK, let directory = panel.url else { return }
        if thenStart {
            await model.openUserTerminal(instance, directory: directory)
        } else {
            model.chooseTerminalDirectory(directory, for: instance)
        }
    }

    private func stopSheet(_ session: UserTerminalSession) -> some View {
        ActionSheet(title: "结束「\(session.instanceID)」的终端？", icon: "stop.circle", tint: .orange) {
            Text("将结束此交互式 CLI 会话。")
        } buttons: {
            Button("取消") { route = nil }.keyboardShortcut(.cancelAction)
            Button("结束会话", role: .destructive) {
                session.stop()
                route = nil
            }
            .accessibilityIdentifier("terminal-confirm-stop")
        }
    }

    private func settingsSheet(_ instance: InstanceRecord) -> some View {
        let pending = model.pending[instance.id]
        let active = model.terminalSession(for: instance)?.phase.isActive == true
        let accountBlocked = instance.busy || pending != nil || active
        return VStack(alignment: .leading, spacing: 12) {
                HStack {
                    SectionLabel(title: "实例设置 · \(instance.id)", symbol: "person.crop.circle")
                        .lineLimit(1)
                    Spacer()
                    Button {
                        Task { await model.refreshInstance(instance) }
                    } label: {
                        HStack(spacing: 5) {
                            ZStack {
                                if pending == Dashboard.providerQueryProgress {
                                    ProgressView().controlSize(.small).scaleEffect(0.7).accessibilityHidden(true)
                                } else {
                                    Image(systemName: "arrow.clockwise")
                                }
                            }
                            .frame(width: 13, height: 13)
                            Text("查询提供方")
                        }
                    }
                    .accessibilityIdentifier("provider-query")
                    .measureFixture("provider-query")
                    .accessibilityValue(pending == Dashboard.providerQueryProgress ? "查询中" : "")
                    .disabled(active || pending != nil || instance.deleting ||
                              instance.login == "authenticating" || instance.login == "auth_interrupted")
                }
                queryDiagnostic(instance, progress: pending, terminalActive: active)
                HStack {
                    Button("登录") { route = .account(instance, "login") }
                        .disabled(accountBlocked || instance.probeFailed || instance.login == "signed_in")
                    Button("退出登录") { route = .account(instance, "logout") }
                        .disabled(accountBlocked || instance.probeFailed || instance.login == "signed_out")
                    Button("切换账户") { route = .account(instance, "switch") }
                        .disabled(accountBlocked || instance.probeFailed || instance.login == "signed_out")
                    if instance.login == "rebind_required" {
                        Button("重绑工作区") { route = .account(instance, "rebind") }.disabled(accountBlocked)
                    }
                    Spacer()
                    Button(role: .destructive) {
                        Task {
                            if let preview = await model.previewDelete(instance) { route = .delete(instance, preview) }
                        }
                    } label: { Label("删除实例", systemImage: "trash") }
                    .disabled(accountBlocked)
                }
                .measureFixture("account-actions")
                HStack {
                    Spacer()
                    Button("完成") { route = nil }.keyboardShortcut(.cancelAction)
                }
                .frame(height: 31)
        }
        .padding(20)
        .frame(width: 490)
        .coordinateSpace(name: "management")
        .overlay(alignment: .bottomLeading) {
            if let notice = model.notice {
                toast(notice, singleLine: true)
                    .frame(maxWidth: 366, alignment: .leading)
                    .padding(.leading, 20)
                    .padding(.bottom, 17)
            }
        }
    }

    private func queryDiagnostic(_ instance: InstanceRecord, progress: String?, terminalActive: Bool) -> some View {
        let transport = model.probeFailure(instance)
        let problem = transport?.detail ?? (instance.probeFailed
            ? instance.probeDiagnostic?.label ?? "查询失败，登录状态未知"
            : nil)
        let summary = problem ?? (instance.login == "auth_interrupted" ? "上次认证中断，请重新登录。"
                                  : terminalActive ? "结束用户终端后可查询提供方或管理账户。"
                                  : !instance.running.isEmpty ? "任务运行期间，账户操作和删除不可用。"
                                  : instance.version ?? "尚无查询记录")
        let checkedAt = transport?.attemptedAt ?? instance.checkedAt
        let timestamps = [checkedAt.map { "最近尝试 \(timestamp($0))" },
                          instance.lastConfirmedAuth.map { "上次确认 \(timestamp($0.checkedAt))" }]
            .compactMap { $0 }.joined(separator: "  ·  ")
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                Image(systemName: problem == nil ? "info.circle" : "exclamationmark.triangle")
                    .accessibilityHidden(true)
                Text(problem == nil || summary.hasPrefix("查询失败") ? summary : "查询失败 · \(summary)")
                    .lineLimit(1)
                Spacer(minLength: 2)
                Text(progress ?? "")
                    .lineLimit(1)
                    .foregroundStyle(.secondary)
            }
            .foregroundStyle(problem == nil ? Color.secondary : legible(.orange, in: scheme))
            Text(timestamps.isEmpty ? "最近尝试 —" : timestamps)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .font(.caption)
        .padding(.horizontal, 8)
        .frame(height: 41, alignment: .leading)
        .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 7))
        .help([summary, timestamps].filter { !$0.isEmpty }.joined(separator: " · "))
        .accessibilityElement(children: .combine)
        .measureFixture("query-diagnostic")
    }

    private var footer: some View {
        HStack(spacing: 6) {
            Image(systemName: model.snapshotError == nil ? "externaldrive" : "exclamationmark.circle")
            if let problem = model.snapshotError {
                Text("本地读取失败 · " + problem)
                    .lineLimit(1)
                    .help(problem + (model.snapshot.map { " · 上次成功 \(timestamp($0.snapshotAt))" } ?? ""))
            } else if let snap = model.snapshot {
                Text("本地快照 " + timestamp(snap.snapshotAt)).lineLimit(1)
            } else {
                Text("正在连接本地 CLI")
            }
            Spacer()
            Text("\(model.signedInCount) / \(model.instances.count) 登录缓存")
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 23)
        .frame(height: 28)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }

    private func toast(_ message: String, singleLine: Bool = false) -> some View {
        HStack(alignment: .center, spacing: 10) {
            Label(message, systemImage: "info.circle")
                .font(.subheadline)
                .lineLimit(singleLine ? 1 : 3)
                .help(message)
                .allowsHitTesting(false)
            Button(action: model.clearNotice) { Image(systemName: "xmark").padding(4) }
                .buttonStyle(.plain)
                .accessibilityLabel("关闭提示")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background {
            RoundedRectangle(cornerRadius: 10).fill(.regularMaterial).allowsHitTesting(false)
        }
        .overlay { RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.13)).allowsHitTesting(false) }
        .shadow(color: .black.opacity(0.16), radius: 8, x: 0, y: 4)
        .frame(maxWidth: 390, alignment: .trailing)
        .accessibilityElement(children: .contain)
    }

    private func deleteSheet(_ instance: InstanceRecord, _ preview: DeletionPreview) -> some View {
        ActionSheet(title: "删除「\(instance.id)」", icon: "trash", tint: .red) {
            if preview.allowed {
                Text("将删除此实例的本地配置和会话数据。此操作无法撤销。")
            } else {
                Text("请先处理以下条件，再重新检查。")
                if preview.requiresLogout { Label("该实例仍绑定账户，请先退出。", systemImage: "person.crop.circle") }
                if !preview.tasks.isEmpty { Text("关联任务 " + preview.tasks.joined(separator: "、")) }
                if !preview.attempts.isEmpty { Text("未结束的尝试 " + preview.attempts.joined(separator: "、")) }
            }
        } buttons: {
            Button(preview.allowed ? "取消" : "关闭") { route = nil }.keyboardShortcut(.cancelAction)
            if preview.allowed {
                Button("确认删除", role: .destructive) {
                    route = nil
                    Task { await model.delete(instance) }
                }
            }
        }
    }

    private func accountSheet(_ instance: InstanceRecord, _ operation: String) -> some View {
        let label = ["login": "登录", "logout": "退出登录", "switch": "切换账户", "rebind": "重绑工作区"][operation] ?? operation
        return ActionSheet(title: "\(label)「\(instance.id)」", icon: "person.crop.circle", tint: .blue) {
            if operation == "rebind" { Text("将把此实例绑定到提供方当前的工作区。") }
            else if operation == "switch" { Text("将在终端退出当前账户，再由 \(instance.provider.name) 启动登录。") }
            else { Text("将在终端由 \(instance.provider.name) 完成操作。") }
            if operation != "login" { Text("绑定到当前账户代次的排队任务需要重新指定。").foregroundStyle(.secondary) }
        } buttons: {
            Button("取消") { route = nil }.keyboardShortcut(.cancelAction)
            Button(operation == "rebind" ? "确认重绑" : "打开终端") {
                route = nil
                Task {
                    if operation == "rebind" { await model.rebind(instance) }
                    else { await model.changeAccount(instance, operation: operation) }
                }
            }
            .buttonStyle(.borderedProminent)
        }
    }
}

@MainActor
private struct LiveInstanceSettings<Content: View>: View {
    @ObservedObject var model: Dashboard
    let key: String
    @ViewBuilder var content: (InstanceRecord) -> Content

    var body: some View {
        if let instance = model.instances.first(where: { $0.terminalKey == key }) {
            content(instance)
        } else {
            Text("实例已不可用").padding(24)
        }
    }
}

struct ActionSheet<Content: View, Buttons: View>: View {
    let title: String
    let icon: String
    let tint: Color
    @ViewBuilder var content: Content
    @ViewBuilder var buttons: Buttons

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(title, systemImage: icon).font(.title3.weight(.semibold)).foregroundStyle(tint)
            ScrollView {
                VStack(alignment: .leading, spacing: 8) { content }.frame(maxWidth: .infinity, alignment: .leading)
            }
            .defaultScrollAnchor(.top)
            .frame(maxHeight: 150)
            .fixedSize(horizontal: false, vertical: true)
            HStack { Spacer(); buttons }
        }
        .padding(22)
        .frame(width: 425)
    }
}

@MainActor
struct CreateSheet: View {
    @ObservedObject var model: Dashboard
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var provider: Provider = .codex
    @State private var parallel = 2
    @State private var binary = ""

    var valid: Bool { name.range(of: "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$", options: .regularExpression) != nil }
    var inProgress: Bool { model.pending[name] != nil }

    var body: some View {
        ActionSheet(title: "新建实例", icon: "plus.circle", tint: .blue) {
            VStack(alignment: .leading, spacing: 9) {
                TextField("实例名称", text: $name, prompt: Text("例如 codex-work"))
                    .textFieldStyle(.roundedBorder)
                Picker("提供方", selection: $provider) {
                    ForEach(Provider.allCases) { provider in Text(provider.name).tag(provider) }
                }
                Stepper("并发基准 \(parallel)", value: $parallel, in: 1...32)
                TextField("CLI 路径（选填）", text: $binary).textFieldStyle(.roundedBorder)
                if let notice = model.notice { Text(notice).font(.caption).foregroundStyle(.secondary) }
            }
            .disabled(inProgress)
        } buttons: {
            Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(inProgress)
            Button("创建") {
                Task {
                    let path = binary.trimmingCharacters(in: .whitespacesAndNewlines)
                    if await model.create(name, provider: provider, parallel: parallel, path: path) { dismiss() }
                }
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
            .disabled(!valid || inProgress)
        }
    }
}


extension View {
    @ViewBuilder
    func measureFixture(_ key: String) -> some View {
        #if DEBUG
        onGeometryChange(for: CGRect.self) { geometry in
            geometry.frame(in: .named("management"))
        } action: { frame in
            VisualEvidence.frames[key] = frame
        }
        #else
        self
        #endif
    }
}
