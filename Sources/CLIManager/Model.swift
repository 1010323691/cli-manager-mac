import Foundation
import SwiftUI

struct Snapshot: Decodable, Sendable {
    let schema: Int
    let snapshotAt: Double
    let instances: [InstanceRecord]
    let liveAttempts: [LiveAttempt]?

    enum CodingKeys: String, CodingKey {
        case schema, instances
        case snapshotAt = "snapshot_at"
        case liveAttempts = "live_attempts"
    }
}

struct LiveAttempt: Decodable, Equatable, Identifiable, Sendable {
    let taskID: String
    let attemptID: String
    let instance: String
    let state: String
    let since: Double?

    var id: String { attemptID }
    var badge: BadgeData { .task(state) }

    enum CodingKeys: String, CodingKey {
        case instance, state, since
        case taskID = "task_id"
        case attemptID = "attempt_id"
    }

    static func normalized(_ records: [Self]) -> [Self] {
        let valid = records.filter { ["starting", "running", "stopping"].contains($0.state) }
        let ranked = valid.sorted {
            if $0.since != $1.since { return ($0.since ?? -.infinity) > ($1.since ?? -.infinity) }
            if $0.taskID != $1.taskID { return $0.taskID < $1.taskID }
            return $0.attemptID < $1.attemptID
        }
        var attempts = Set<String>(), tasks = Set<String>()
        return ranked.filter {
            guard !attempts.contains($0.attemptID), !tasks.contains($0.taskID) else { return false }
            attempts.insert($0.attemptID)
            tasks.insert($0.taskID)
            return true
        }
    }
}

enum Provider: String, Decodable, CaseIterable, Identifiable, Sendable {
    case codex, claude
    var id: Self { self }
    var name: String { self == .codex ? "Codex" : "Claude Code" }
    var symbol: String { self == .codex ? "chevron.left.forwardslash.chevron.right" : "asterisk" }
    var tint: Color { self == .codex ? Color(red: 0.44, green: 0.74, blue: 0.99) : Color(red: 1, green: 0.65, blue: 0.42) }
}

struct ConfirmedAuth: Decodable, Sendable {
    let login: String
    let checkedAt: Double

    enum CodingKeys: String, CodingKey {
        case login
        case checkedAt = "checked_at"
    }
}

struct ProbeDiagnostic: Decodable, Sendable {
    let stage: String
    let reason: String

    var label: String {
        let phase = ["initialize": "启动 App Server", "account": "读取账户", "models": "读取模型",
                     "features": "读取功能", "version": "读取 CLI 版本", "auth_status": "读取账户",
                     "quota": "读取额度"][stage] ?? "查询提供方"
        let detail = ["permission_denied": "进程或文件权限受限", "network_unavailable": "网络不可达",
                      "process_exited": "提供方进程退出", "rpc_timeout": "提供方未响应",
                      "subprocess_timeout": "进程超时", "invalid_response": "响应格式无法识别",
                      "executable_missing": "找不到 CLI"][reason] ?? "提供方返回错误"
        return "\(phase) · \(detail)"
    }
}

struct InstanceRecord: Decodable, Identifiable, Sendable {
    let id: String
    let guardToken: String
    let registration: String?
    let quotaGroup: String?
    let quotaUsage: QuotaUsage?
    let provider: Provider
    let parallel: Int
    let login: String
    let loginIsStale: Bool?
    let checkedAt: Double?
    let version: String?
    let plan: String?
    let probeFailed: Bool
    let lastConfirmedAuth: ConfirmedAuth?
    let probeDiagnostic: ProbeDiagnostic?
    let deleting: Bool
    let running: [Activity]
    let quota: Quota

    enum CodingKeys: String, CodingKey {
        case id, registration, provider, parallel, login, version, plan, deleting, running, quota
        case guardToken = "guard"
        case quotaGroup = "quota_group"
        case quotaUsage = "quota_usage"
        case loginIsStale = "login_is_stale"
        case checkedAt = "checked_at"
        case probeFailed = "probe_failed"
        case lastConfirmedAuth = "last_confirmed_auth"
        case probeDiagnostic = "probe_diagnostic"
    }

    var terminalKey: String { registration ?? guardToken }

    var busy: Bool { !running.isEmpty || login == "authenticating" || deleting }
    var activity: BadgeData {
        if deleting { return BadgeData("删除中", "trash", .orange) }
        if running.contains(where: { $0.state == "stopping" }) { return BadgeData("停止中", "stop.circle", .orange) }
        if running.contains(where: { $0.state == "running" }) { return BadgeData("执行中", "play.circle.fill", .green) }
        if running.contains(where: { $0.state == "starting" }) { return BadgeData("启动中", "hourglass", .blue) }
        if !running.isEmpty { return BadgeData("运行状态待核实", "questionmark.circle", .orange) }
        if quota.state == "limited" { return BadgeData("额度耗尽", "exclamationmark.octagon.fill", .red) }
        if probeFailed { return BadgeData("查询失败", "exclamationmark.triangle", .orange) }
        if login != "signed_in" { return loginBadge }
        return BadgeData("空闲", "pause.circle", .secondary)
    }
    var loginBadge: BadgeData {
        if probeFailed { return BadgeData("查询失败，登录状态未知", "exclamationmark.triangle", .orange) }
        return switch login {
        case "signed_in": BadgeData(loginIsStale == true ? "上次已登录" : "已登录",
                                    "person.crop.circle.badge.checkmark", loginIsStale == true ? .secondary : .green)
        case "signed_out": BadgeData(loginIsStale == true ? "上次未登录" : "未登录",
                                     "person.crop.circle.badge.minus", .secondary)
        case "expired": BadgeData("登录失效", "person.crop.circle.badge.exclamationmark", .orange)
        case "unbound": BadgeData("账户未绑定", "person.crop.circle.badge.exclamationmark", .orange)
        case "authenticating": BadgeData("认证中", "hourglass", .blue)
        case "auth_interrupted": BadgeData("认证中断", "person.crop.circle.badge.exclamationmark", .orange)
        case "rebind_required": BadgeData("待重绑工作区", "link.badge.plus", .orange)
        case "invalidated": BadgeData("登录待复核", "arrow.trianglehead.clockwise", .orange)
        default: BadgeData("登录未知", "questionmark.circle", .secondary)
        }
    }
}

struct Activity: Decodable, Identifiable, Sendable {
    let id: String
    let state: String
}

struct Quota: Decodable, Sendable {
    let state: String
    let remaining: Double?
    let observedAt: Double?
    let checkedAt: Double?
    let blockedAt: Double?
    let cooldownUntil: Double?
    let limitResetAt: Double?
    let resetEvents: [QuotaResetEvent]?
    let windows: [QuotaWindow]
    let unverifiedWindows: [QuotaWindow]?

    var displayWindows: [QuotaWindow] { windows + (unverifiedWindows ?? []) }

    enum CodingKeys: String, CodingKey {
        case state, windows
        case unverifiedWindows = "unverified_windows"
        case remaining = "remaining_percent"
        case observedAt = "observed_at"
        case checkedAt = "checked_at"
        case blockedAt = "blocked_at"
        case cooldownUntil = "cooldown_until"
        case limitResetAt = "limit_reset_at"
        case resetEvents = "reset_events"
    }

    var label: String {
        switch state {
        case "limited": "额度耗尽"
        case "expired": "待刷新"
        case "observed": remaining.map { "\($0.formatted(.number.precision(.fractionLength(0...1))))%" } ?? "未知"
        default: "未知"
        }
    }

    var tint: Color {
        switch state {
        case "limited": .red
        case "expired": .orange
        case "observed": (remaining ?? 100) < 20 ? .orange : .green
        default: .secondary
        }
    }
}

struct QuotaWindow: Decodable, Identifiable, Sendable {
    let bucket: String?
    let window: String?
    let remaining: Double?
    let observedAt: Double
    let resetsAt: Double?
    let minutes: Double?
    let expired: Bool
    var refreshReason: String? = nil

    enum CodingKeys: String, CodingKey {
        case bucket, window, minutes, expired
        case refreshReason = "refresh_reason"
        case remaining = "remaining_percent"
        case observedAt = "observed_at"
        case resetsAt = "resets_at"
    }

    var id: String { "\(bucket ?? "")/\(window ?? "")" }
    var name: String {
        let scope = bucket.flatMap { ["codex", "claude"].contains($0) ? nil : $0 }
        let duration: String?
        if let minutes, minutes > 0, minutes < 1_000_000, minutes.rounded() == minutes {
            let value = Int(minutes)
            if value.isMultiple(of: 1_440) { duration = "\(value / 1_440) 天" }
            else if value.isMultiple(of: 60) { duration = "\(value / 60) 小时" }
            else { duration = "\(value) 分钟" }
        } else { duration = nil }
        let label = duration ?? ["primary": "主要窗口", "secondary": "次要窗口", "session": "会话", "week": "周额度"][window ?? ""]
            ?? window ?? "额度窗口"
        return [scope, label].compactMap { $0 }.joined(separator: " · ")
    }
}

struct DeletionPreview: Decodable, Sendable {
    let instance: String
    let attempts: [String]
    let tasks: [String]
    let requiresLogout: Bool
    let deleted: Bool
    var allowed: Bool { attempts.isEmpty && tasks.isEmpty && !requiresLogout }

    enum CodingKeys: String, CodingKey {
        case instance, attempts, tasks, deleted
        case requiresLogout = "requires_logout"
    }
}

struct CommandResult: Decodable, Sendable {
    let instance: String
    let completed: Bool
    let probeFailed: Bool?
    let quotaObserved: Bool?

    enum CodingKeys: String, CodingKey {
        case instance, completed
        case probeFailed = "probe_failed"
        case quotaObserved = "quota_observed"
    }
}

struct BadgeData {
    let title: String
    let icon: String
    let color: Color

    init(_ title: String, _ icon: String, _ color: Color) {
        self.title = title
        self.icon = icon
        self.color = color
    }

    static func task(_ state: String) -> BadgeData {
        switch state {
        case "running": BadgeData("运行中", "play.circle.fill", .green)
        case "starting": BadgeData("启动中", "hourglass", .blue)
        case "stopping": BadgeData("停止中", "stop.circle", .orange)
        case "queued": BadgeData("排队中", "clock", .blue)
        case "waiting_quota": BadgeData("等待额度", "clock.badge.exclamationmark", .orange)
        case "review": BadgeData("待审核", "checklist", .blue)
        case "approved": BadgeData("已批准", "checkmark.seal", .green)
        case "applied": BadgeData("已交付", "checkmark.circle.fill", .green)
        case "applying": BadgeData("应用中", "arrow.down.circle", .blue)
        case "cancelled", "discarded": BadgeData("已停止", "stop.circle", .secondary)
        case "interrupted": BadgeData("已中断", "exclamationmark.circle", .orange)
        case "failed": BadgeData("失败", "xmark.circle", .red)
        case "auth": BadgeData("需要认证", "person.crop.circle.badge.exclamationmark", .orange)
        case "unverified": BadgeData("运行待核实", "questionmark.circle", .orange)
        case "stale": BadgeData("已过期", "clock.badge.exclamationmark", .orange)
        case "needs_report": BadgeData("缺少报告", "doc.badge.ellipsis", .orange)
        default: BadgeData(state, "circle", .secondary)
        }
    }
}

@MainActor
final class Dashboard: ObservableObject {
    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var snapshotError: String?
    @Published private(set) var pending: [String: String] = [:]
    @Published private(set) var notice: String?
    private var quotaTriggers = QuotaLifecycle()
    private var startupQuotaGroups: Set<String>?
    private var quotaInFlight: Set<String> = []
    private var autoQuotaTask: Task<Void, Never>?
    private var resetRetries: [String: QuotaResetRetries] = [:]
    @Published private var probeFailures: [String: ProbeFailure] = [:]

    static let providerQueryProgress = "正在查询提供方"

    struct ProbeFailure {
        let guardToken: String
        let attemptedAt: Double
        let detail: String
    }
    @Published var selection: String?
    @Published var edgeEnabled = true
    private var runtime: Runtime?
    private let reloads = SnapshotReload()
    @Published private var terminalSessions: [String: UserTerminalSession] = [:]
    @Published private var presentedManualTerminal: UUID?
    @Published private var terminalDirectories: [String: String]
    private let preferences: UserDefaults?
    private let stateDirectory: URL

    init() {
        let environment = ProcessInfo.processInfo.environment
        let state = environment["CLI_MANAGER_STATE"] ?? environment["CLI_DELEGATE_V2_STATE"]
        preferences = state == nil ? .standard : nil
        selection = preferences?.string(forKey: "selectedInstance")
        terminalDirectories = preferences?.dictionary(forKey: "userTerminalDirectories") as? [String: String] ?? [:]
        let path = state.map { ($0 as NSString).expandingTildeInPath }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cli-delegate-v2").path
        stateDirectory = URL(fileURLWithPath: path, isDirectory: true)
    }

    var instances: [InstanceRecord] { snapshot?.instances ?? [] }
    var selected: InstanceRecord? { instances.first { $0.id == selection } }
    private var lastKnownAttempts: [LiveAttempt] {
        LiveAttempt.normalized(snapshot?.liveAttempts ?? [])
    }

    var liveAttempts: [LiveAttempt] {
        snapshotError == nil ? lastKnownAttempts : []
    }

    func managedAttempts(for instance: InstanceRecord) -> [LiveAttempt] {
        // A failed refresh cannot implicitly switch a read-only workspace to a PTY.
        lastKnownAttempts.filter { $0.instance == instance.id }
    }

    func managedOutput(for attempt: LiveAttempt) async throws -> ManagedOutput {
        let runtime = try await connection()
        let result: ManagedOutput = try await runtime.run(
            ["managed-output", attempt.taskID, attempt.attemptID, attempt.instance], timeout: 4
        )
        guard result.schema == 1, result.attemptID == attempt.attemptID else {
            throw CLIError.configuration("无法核实托管任务输出。")
        }
        return result
    }

    var signedInCount: Int { instances.filter { $0.login == "signed_in" }.count }

    func choose(_ instance: String?) {
        if instance != selection { presentedManualTerminal = nil }
        selection = instance
        preferences?.set(instance, forKey: "selectedInstance")
    }

    func refresh() async {
        await reloads.request { [weak self] isCurrent in
            guard let self else { return }
            do {
                let runtime = try await connection()
                let value: Snapshot = try await runtime.run(["snapshot"])
                guard isCurrent() else { return }
                guard value.schema == 1 else { throw CLIError.configuration("CLI 快照版本不受支持。") }
                observeQuotaTriggers(value.instances)
                snapshot = value
                snapshotError = nil
                if selected == nil, selection == nil || !value.instances.isEmpty { choose(value.instances.first?.id) }
            } catch {
                if isCurrent() { snapshotError = error.localizedDescription }
            }
        }
    }

    var runningUserTerminals: Bool { terminalSessions.values.contains { $0.phase.isActive } }

    func terminalSession(for instance: InstanceRecord) -> UserTerminalSession? { terminalSessions[instance.terminalKey] }

    func prefersManualTerminal(_ session: UserTerminalSession?) -> Bool {
        guard let session, session.phase.isActive else { return false }
        return presentedManualTerminal == session.id
    }

    func presentManualTerminal(_ session: UserTerminalSession?) {
        presentedManualTerminal = session?.id
    }

    func terminalDirectory(for instance: InstanceRecord) -> URL? {
        if let session = terminalSession(for: instance), session.phase.isActive { return session.directory }
        guard let path = terminalDirectories[instance.terminalKey], path.hasPrefix("/") else { return nil }
        return TerminalDirectory.validated(URL(fileURLWithPath: path, isDirectory: true), state: stateDirectory)
    }

    @discardableResult
    func chooseTerminalDirectory(_ candidate: URL, for instance: InstanceRecord) -> URL? {
        guard instances.contains(where: { $0.terminalKey == instance.terminalKey }),
              terminalSession(for: instance)?.phase.isActive != true else { return nil }
        guard let directory = TerminalDirectory.validated(candidate, state: stateDirectory) else {
            notice = "请选择存在且与 CLI Manager 状态目录不重叠的工作目录。"
            return nil
        }
        terminalDirectories[instance.terminalKey] = directory.path
        preferences?.set(terminalDirectories, forKey: "userTerminalDirectories")
        return directory
    }

    func terminalStartBlocked(_ instance: InstanceRecord) -> Bool {
        pending[instance.id] != nil || instance.deleting ||
            ["authenticating", "auth_interrupted", "signed_out", "expired", "unbound", "rebind_required"].contains(instance.login)
    }

    func openUserTerminal(_ instance: InstanceRecord, directory: URL) async {
        guard let current = instances.first(where: { $0.terminalKey == instance.terminalKey }),
              current.guardToken == instance.guardToken, !terminalStartBlocked(current),
              let validated = chooseTerminalDirectory(directory, for: current) else { return }
        let session = UserTerminalSession(instanceID: current.id, directory: validated) { [weak self] in
            self?.objectWillChange.send()
        }
        terminalSessions[current.terminalKey] = session
        notice = nil
        do {
            let runtime = try await connection()
            session.start(runtime.userTerminalLaunch(current, directory: validated))
        } catch {
            session.failToStart(error.localizedDescription)
        }
    }

    func stopAllUserTerminals() {
        for session in terminalSessions.values { session.stop() }
    }

    func probeFailure(_ instance: InstanceRecord) -> ProbeFailure? {
        guard let failure = probeFailures[instance.id], failure.guardToken == instance.guardToken,
              failure.attemptedAt > (instance.checkedAt ?? 0) else { return nil }
        return failure
    }

    func refreshInstance(_ instance: InstanceRecord) async {
        _ = await queryQuota(instance, announce: true)
    }

    private enum QuotaQuery { case observed, noData, failed, skipped }

    private func queryQuota(_ instance: InstanceRecord, announce: Bool) async -> QuotaQuery {
        guard terminalSession(for: instance)?.phase.isActive != true, pending[instance.id] == nil else { return .skipped }
        let key = instance.quotaGroup ?? instance.terminalKey
        guard quotaInFlight.insert(key).inserted else { return .skipped }
        defer { quotaInFlight.remove(key) }
        var outcome = QuotaQuery.failed
        await perform(instance.id, Self.providerQueryProgress, announce: announce) { runtime in
            let attemptedAt = Date.now.timeIntervalSince1970
            do {
                let result: CommandResult = try await runtime.run(
                    ["refresh", instance.id, "--expect", instance.guardToken], timeout: 125
                )
                if !result.completed {
                    outcome = .skipped
                    return "「\(instance.id)」状态在查询期间变化。请重新查询。"
                }
                self.probeFailures[instance.id] = nil
                if result.probeFailed == true {
                    return "「\(instance.id)」查询失败，登录状态未知。"
                }
                if result.quotaObserved != true {
                    outcome = .noData
                    return "「\(instance.id)」未返回新额度。"
                }
                outcome = .observed
                return "已查询「\(instance.id)」的额度。"
            } catch {
                self.probeFailures[instance.id] = ProbeFailure(guardToken: instance.guardToken, attemptedAt: attemptedAt,
                                                              detail: error.localizedDescription)
                throw error
            }
        }
        return outcome
    }

    private func hasQuotaAccount(_ instance: InstanceRecord) -> Bool {
        instance.quotaGroup != nil &&
            (instance.login == "signed_in" || (instance.probeFailed && instance.lastConfirmedAuth?.login == "signed_in"))
    }

    private func observeQuotaTriggers(_ records: [InstanceRecord]) {
        let accounts = Set(records.filter(hasQuotaAccount).compactMap(\.quotaGroup))
        if startupQuotaGroups == nil { startupQuotaGroups = accounts }
        else { startupQuotaGroups?.formIntersection(accounts) }
        quotaTriggers.observe(Dictionary(uniqueKeysWithValues: records.map { ($0.guardToken, $0.quotaUsage ?? .empty) }))
        for group in Array(resetRetries.keys) {
            if accounts.contains(group) {
                let events = Set(records.filter { $0.quotaGroup == group }.flatMap { $0.quota.resetEvents ?? [] })
                resetRetries[group]?.retain(events)
            } else {
                resetRetries.removeValue(forKey: group)
            }
        }
    }

    private func quotaEligible(_ instance: InstanceRecord) -> Bool {
        guard hasQuotaAccount(instance), let group = instance.quotaGroup, !quotaInFlight.contains(group),
              pending[instance.id] == nil else { return false }
        return !instances.contains {
            $0.quotaGroup == group && ($0.deleting || $0.login == "authenticating" ||
                $0.login == "auth_interrupted" || terminalSession(for: $0)?.phase.isActive == true)
        }
    }

    func scheduleAutomaticQuotas() {
        guard snapshotError == nil, autoQuotaTask == nil else { return }
        let now = Date.now.timeIntervalSince1970
        let eligible = instances.filter(quotaEligible).sorted { lhs, rhs in
            if lhs.probeFailed != rhs.probeFailed { return !lhs.probeFailed }
            return lhs.id < rhs.id
        }
        func isDue(_ instance: InstanceRecord) -> Bool {
            guard let group = instance.quotaGroup else { return false }
            let retries = resetRetries[group] ?? QuotaResetRetries()
            return instance.quota.resetEvents?.contains { retries.allows($0, now: now) } == true
        }
        let target = eligible.first { quotaTriggers.pending[$0.guardToken] != nil }
            ?? eligible.first { $0.quotaGroup.map { startupQuotaGroups?.contains($0) == true } == true }
            ?? eligible.first(where: isDue)
        guard let target, let group = target.quotaGroup else { return }
        let event = quotaTriggers.pending[target.guardToken]
        let resets = (target.quota.resetEvents ?? []).filter { $0.resetsAt <= now }
        autoQuotaTask = Task { [weak self] in
            guard let self else { return }
            let result = await queryQuota(target, announce: false)
            autoQuotaTask = nil
            // If the binding changed or a concurrent operation acquired the
            // profile, let the next snapshot choose a fresh candidate.
            guard result != .skipped else { return }
            startupQuotaGroups?.remove(group)
            if let event { quotaTriggers.complete(target.guardToken, through: event) }
            if !resets.isEmpty {
                resetRetries[group, default: QuotaResetRetries()].record(resets, now: Date.now.timeIntervalSince1970)
            }
            if !Task.isCancelled { scheduleAutomaticQuotas() }
        }
    }

    func create(_ name: String, provider: Provider, parallel: Int, path: String) async -> Bool {
        var args = ["create", name, "--backend", provider.rawValue, "--parallel", String(parallel)]
        if !path.isEmpty { args += ["--bin", path] }
        return await perform(name, "正在创建实例") { runtime in
            let _: CommandResult = try await runtime.run(args)
            self.choose(name)
            return "已创建「\(name)」。"
        }
    }

    func previewDelete(_ instance: InstanceRecord) async -> DeletionPreview? {
        var preview: DeletionPreview?
        await perform(instance.id, "正在检查删除条件") { runtime in
            preview = try await runtime.run(["delete", instance.id, "--expect", instance.guardToken])
            return nil
        }
        return preview
    }

    func delete(_ instance: InstanceRecord) async {
        guard terminalSession(for: instance)?.phase.isActive != true else { return }
        await perform(instance.id, "正在删除实例") { runtime in
            let result: DeletionPreview = try await runtime.run(["delete", instance.id, "--expect", instance.guardToken, "--confirm"])
            if result.deleted {
                self.terminalSessions[instance.terminalKey] = nil
                self.terminalDirectories[instance.terminalKey] = nil
                self.preferences?.set(self.terminalDirectories, forKey: "userTerminalDirectories")
                return "已删除「\(instance.id)」。"
            }
            return nil
        }
    }

    func changeAccount(_ instance: InstanceRecord, operation: String) async {
        guard terminalSession(for: instance)?.phase.isActive != true else { return }
        let succeeded = await perform(instance.id, "请在终端完成账户操作") { runtime in
            try await runtime.authenticate(operation, instance: instance)
            return "「\(instance.id)」的账户操作已完成。"
        }
        if succeeded, let current = instances.first(where: { $0.id == instance.id }) {
            await refreshInstance(current)
        }
    }

    func rebind(_ instance: InstanceRecord) async {
        guard terminalSession(for: instance)?.phase.isActive != true else { return }
        await perform(instance.id, "正在重绑工作区") { runtime in
            let _: CommandResult = try await runtime.run(["rebind", instance.id, "--expect", instance.guardToken], timeout: 125)
            return "已重绑「\(instance.id)」的工作区。"
        }
    }

    func clearNotice() { notice = nil }

    @discardableResult
    private func perform(_ key: String, _ progress: String, announce: Bool = true,
                         work: (Runtime) async throws -> String?) async -> Bool {
        guard pending[key] == nil else { return false }
        pending[key] = progress
        if announce { notice = nil }
        defer { pending[key] = nil }
        do {
            let runtime = try await connection()
            let message = try await work(runtime)
            if announce { notice = message }
            await refresh()
            return true
        } catch {
            if announce { notice = error.localizedDescription }
            await refresh()
            return false
        }
    }

    private func connection() async throws -> Runtime {
        if let runtime { return runtime }
        let runtime = try await Task.detached(priority: .userInitiated) { try Runtime.discover() }.value
        self.runtime = runtime
        return runtime
    }
}

func timestamp(_ seconds: Double) -> String {
    Date(timeIntervalSince1970: seconds).formatted(date: .abbreviated, time: .standard)
}
