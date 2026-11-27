import AppKit
import SwiftUI

@main
@MainActor
struct CLIManagerApplication {
    static func main() {
        let application = NSApplication.shared
        let controller = ApplicationController()
        application.delegate = controller
        application.setActivationPolicy(.accessory)
        application.run()
        withExtendedLifetime(controller) {}
    }
}

@MainActor
private final class ApplicationController: NSObject, NSApplicationDelegate {
    private let dashboard = Dashboard()
    private var status: NSStatusItem?
    private var statusEdgeToggle: NSMenuItem?
    private var window: NSWindow?
    private var refreshTask: Task<Void, Never>?
    private var quitTask: Task<Void, Never>?
    private var spaceObserver: NSObjectProtocol?
    private lazy var edge = EdgeController(model: dashboard) { [weak self] in
        self?.showManagement()
    }
    private var preferences: UserDefaults? {
        let environment = ProcessInfo.processInfo.environment
        return (environment["CLI_MANAGER_STATE"] ?? environment["CLI_DELEGATE_V2_STATE"]) == nil ? .standard : nil
    }
    private var edgeEnabled: Bool {
        get { dashboard.edgeEnabled }
        set {
            dashboard.edgeEnabled = newValue
            edge.enabled = newValue
            statusEdgeToggle?.state = newValue ? .on : .off
            preferences?.set(newValue, forKey: "edgeEnabled")
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMainMenu()
        configureStatus()
        if let saved = preferences?.object(forKey: "edgeEnabled") as? Bool { edgeEnabled = saved }
        edge.start()
        spaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.edge.spaceChanged() } }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.dashboard.refresh()
                self?.dashboard.scheduleAutomaticQuotas()
                try? await Task.sleep(for: .seconds(8))
            }
        }
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("--manage") || !arguments.contains("--menu-bar") { showManagement() }
        #if DEBUG
        if let index = arguments.firstIndex(of: "--capture-fixture"), arguments.indices.contains(index + 1) {
            let destination = URL(fileURLWithPath: arguments[index + 1], isDirectory: true)
            Task { await captureFixture(into: destination) }
        }
        if let index = arguments.firstIndex(of: "--capture-activity-fixture"), arguments.indices.contains(index + 1) {
            let destination = URL(fileURLWithPath: arguments[index + 1], isDirectory: true)
            Task { await captureActivityFixture(into: destination) }
        }
        if let index = arguments.firstIndex(of: "--capture-dock-fixture"), arguments.indices.contains(index + 1) {
            let destination = URL(fileURLWithPath: arguments[index + 1], isDirectory: true)
            Task { await captureDockFixture(into: destination) }
        }
        if let index = arguments.firstIndex(of: "--capture-status-fixture"), arguments.indices.contains(index + 1) {
            let destination = URL(fileURLWithPath: arguments[index + 1], isDirectory: true)
            Task { await captureStatusFixture(into: destination) }
        }
        #endif
    }

    func applicationDidChangeScreenParameters(_ notification: Notification) { edge.rebuild() }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showManagement()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitTask != nil { return .terminateLater }
        guard dashboard.runningUserTerminals else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "退出 CLI Manager？"
        alert.informativeText = "退出会结束仍在运行的用户终端会话。"
        alert.addButton(withTitle: "退出")
        alert.addButton(withTitle: "取消").keyEquivalent = "\u{1b}"
        guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        quitTask = Task { [weak self] in
            guard let self else { return }
            let stopped = await finishUserTerminals()
            quitTask = nil
            sender.reply(toApplicationShouldTerminate: stopped)
            if !stopped {
                let message = NSAlert()
                message.messageText = "仍有用户终端正在结束"
                message.informativeText = "会话结束后可再次退出 CLI Manager。"
                message.runModal()
            }
        }
        return .terminateLater
    }

    private func finishUserTerminals() async -> Bool {
        dashboard.stopAllUserTerminals()
        let deadline = ContinuousClock.now + .seconds(6)
        while dashboard.runningUserTerminals, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
        return !dashboard.runningUserTerminals
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTask?.cancel()
        if let spaceObserver { NSWorkspace.shared.notificationCenter.removeObserver(spaceObserver) }
        edge.stop()
    }

    #if DEBUG
    private func captureActivityFixture(into directory: URL) async {
        do {
            let environment = ProcessInfo.processInfo.environment
            guard let state = environment["CLI_MANAGER_STATE"],
                  FileManager.default.fileExists(atPath: state + "/.activity-ui-fixture") else {
                throw VisualEvidence.failure("Capture requires the isolated activity fixture")
            }
            let stateURL = URL(fileURLWithPath: state, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            showManagement()
            window?.appearance = NSAppearance(named: .aqua)
            try await waitForFixture("instances") { self.dashboard.instances.count == 7 }
            dashboard.choose("codex-idle")
            try await VisualEvidence.settle()
            let keys = ["toolbar", "workspace", "provider-query", "query-diagnostic", "account-actions"]
                + dashboard.instances.map { "instance-\($0.id)" }
            if let instance = dashboard.selected, let window {
                let frame = window.frame
                window.contentViewController = NSHostingController(rootView:
                    Management(model: dashboard, edgeEnabled: .constant(true), settingsOnOpen: instance))
                window.setFrame(frame, display: true)
            }
            try await VisualEvidence.settle()
            let reference = try fixtureFrames(keys)
            var measured = ["before": reference]
            let view = window?.contentView
            let profile = stateURL.appendingPathComponent("instances/codex-idle/cli", isDirectory: true)
            let entered = profile.appendingPathComponent("probe-entered")
            let release = profile.appendingPathComponent("probe-release")
            for phase in ["success", "failure"] {
                try? FileManager.default.removeItem(at: entered)
                try? FileManager.default.removeItem(at: release)
                try Data("wait-\(phase)".utf8).write(to: profile.appendingPathComponent("fixture-mode"), options: .atomic)
                guard let selected = dashboard.selected else { throw VisualEvidence.failure("Selected fixture is missing") }
                let query = Task { await dashboard.refreshInstance(selected) }
                try await waitForFixture("provider entered wait") { FileManager.default.fileExists(atPath: entered.path) }
                try await VisualEvidence.settle()
                measured["during-\(phase)"] = try fixtureFrames(keys)
                let settings = window?.attachedSheet?.contentView ?? view
                try VisualEvidence.capture(settings, to: directory.appendingPathComponent("query-during-\(phase).png"))
                try Data().write(to: release)
                await query.value
                try await VisualEvidence.settle()
                guard let current = dashboard.selected, current.probeFailed == (phase == "failure"),
                      (current.checkedAt ?? 0) > (selected.checkedAt ?? 0), dashboard.notice != nil else {
                    throw VisualEvidence.failure("Missing fresh \(phase) result and toast")
                }
                measured["after-\(phase)"] = try fixtureFrames(keys)
                try VisualEvidence.capture(settings, to: directory.appendingPathComponent("query-\(phase).png"))
                dashboard.clearNotice()
                try await VisualEvidence.settle()
                if phase == "failure" {
                    try VisualEvidence.capture(settings, to: directory.appendingPathComponent("query-diagnostic.png"))
                }
            }
            guard measured.values.allSatisfy({ $0 == reference }) else {
                let data = try JSONSerialization.data(withJSONObject: measured, options: [.prettyPrinted, .sortedKeys])
                try data.write(to: directory.appendingPathComponent("query-geometry.json"))
                throw VisualEvidence.failure("Controls moved across query transitions")
            }
            let data = try JSONSerialization.data(withJSONObject: measured, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: directory.appendingPathComponent("query-geometry.json"))
            if let window {
                if let settings = window.attachedSheet { window.endSheet(settings) }
                let frame = window.frame
                window.contentViewController = NSHostingController(rootView:
                    Management(model: dashboard, edgeEnabled: .constant(true)))
                window.setFrame(frame, display: true)
            }
            let phaseFile = stateURL.appendingPathComponent("activity-phase")
            var panelFrame: CGRect?
            for (phase, title, expected) in [
                ("empty", "activity-empty-initial", []),
                ("active", "activity-two", ["review-interface", "audit-bridge"]),
                ("one", "activity-one", ["audit-bridge"]),
                ("empty", "activity-empty", []),
            ] {
                let previous = dashboard.snapshot?.snapshotAt ?? 0
                try Data(phase.utf8).write(to: phaseFile, options: .atomic)
                try await waitForFixture("automatic snapshot \(title)", seconds: 12) {
                    (self.dashboard.snapshot?.snapshotAt ?? 0) > previous && self.dashboard.liveAttempts.map(\.taskID) == expected
                }
                if phase == "active" {
                    VisualEvidence.frames.removeValue(forKey: "managed-output")
                    dashboard.choose("codex-active")
                    try await waitForFixture("managed output in the selected workspace") {
                        VisualEvidence.frames["managed-output"] != nil
                    }
                    guard let attempt = dashboard.liveAttempts.first(where: { $0.instance == "codex-active" }),
                          try await dashboard.managedOutput(for: attempt).entries.contains(where: {
                              $0.text == "正在检查测试工作区。"
                          }) else {
                        throw VisualEvidence.failure("Missing verified managed activity fixture")
                    }
                    window?.makeKeyAndOrderFront(nil)
                    try await VisualEvidence.settle()
                    let current = window?.contentView
                    try VisualEvidence.capture(current?.superview ?? current, to: directory.appendingPathComponent("mixed-sidebar.png"))
                }
                let frame = try await edge.captureActivityFixture(to: directory.appendingPathComponent("\(title).png"))
                if let panelFrame, panelFrame != frame { throw VisualEvidence.failure("Activity panel changed frame") }
                panelFrame = frame
                if phase == "active" {
                    dashboard.choose("codex-signed-out")
                    guard dashboard.liveAttempts.map(\.taskID) == expected else {
                        throw VisualEvidence.failure("Selection changed activity")
                    }
                    _ = try await edge.captureActivityFixture(to: directory.appendingPathComponent("activity-two-after-selection.png"))
                }
            }
            try Data("Stable native coordinates. Automatic activity 0 → 2 → 1 → 0; selection-independent.\n".utf8)
                .write(to: directory.appendingPathComponent("activity-result"), options: .atomic)
        } catch {
            try? Data(error.localizedDescription.utf8).write(to: directory.appendingPathComponent("activity-result"), options: .atomic)
        }
        NSApp.terminate(nil)
    }

    private func waitForFixture(_ label: String, seconds: TimeInterval = 5, until predicate: () -> Bool) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + seconds
        while !predicate() {
            if ProcessInfo.processInfo.systemUptime > deadline {
                throw VisualEvidence.failure("Timed out waiting for \(label)")
            }
            try await Task.sleep(for: .milliseconds(75))
        }
    }

    private func fixtureFrames(_ keys: [String]) throws -> [String: [CGFloat]] {
        try Dictionary(uniqueKeysWithValues: keys.map { key in
            guard let rect = VisualEvidence.frames[key], !rect.isEmpty else {
                throw VisualEvidence.failure("Missing AppKit geometry for \(key)")
            }
            return (key, [rect.minX, rect.minY, rect.width, rect.height])
        })
    }

    private func captureManagementFixture(to file: URL) throws {
        let view = window?.contentView
        try VisualEvidence.capture(view?.superview ?? view, to: file, backdrop: WorkspacePalette.native(WorkspacePalette.paper).cgColor)
    }

    private func captureDockFixture(into directory: URL) async {
        do {
            let environment = ProcessInfo.processInfo.environment
            guard let state = environment["CLI_MANAGER_STATE"],
                  FileManager.default.fileExists(atPath: state + "/.dock-ui-fixture") else {
                throw VisualEvidence.failure("Capture requires the isolated dock fixture")
            }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let root = URL(fileURLWithPath: state).deletingLastPathComponent()
            let aDirectory = root.appendingPathComponent("Project Alpha", isDirectory: true)
            let bDirectory = root.appendingPathComponent("Project Beta", isDirectory: true)
            NSApp.appearance = NSAppearance(named: .darkAqua)
            showManagement()
            guard NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua,
                  window?.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .aqua else {
                throw VisualEvidence.failure("Light workspace did not hold under a dark application appearance")
            }
            try await waitForFixture("dock registry") { self.dashboard.instances.count == 7 }
            guard let alpha = dashboard.instances.first(where: { $0.id == "codex-active" }),
                  let beta = dashboard.instances.first(where: { $0.id == "codex-idle" }) else {
                throw VisualEvidence.failure("Missing dock test registrations")
            }
            dashboard.choose(alpha.id)
            try await VisualEvidence.settle()
            guard !dashboard.runningUserTerminals, dashboard.terminalDirectory(for: alpha) == nil else {
                throw VisualEvidence.failure("Selection started a terminal or inferred a working directory")
            }
            try captureManagementFixture(to: directory.appendingPathComponent("terminal-idle.png"))
            await dashboard.openUserTerminal(alpha, directory: aDirectory)
            let a = try await dockFixtureSession(alpha, directory: aDirectory)
            try captureManagementFixture(to: directory.appendingPathComponent("terminal-first.png"))
            sendDockFixture("y\n", to: a)
            try await waitForFixture("Alpha permission") { self.receipt(aDirectory)?["permission"] as? Bool == true }

            dashboard.choose(beta.id)
            try await VisualEvidence.settle()
            guard a.terminal.window == nil, a.phase.isActive, dashboard.terminalSession(for: beta) == nil else {
                throw VisualEvidence.failure("Selection unexpectedly ended Alpha or started Beta")
            }
            await dashboard.openUserTerminal(beta, directory: bDirectory)
            let b = try await dockFixtureSession(beta, directory: bDirectory)
            await dashboard.refreshInstance(alpha)
            guard dashboard.probeFailure(alpha) == nil, dashboard.pending[alpha.id] == nil else {
                throw VisualEvidence.failure("An active terminal was incorrectly treated as a provider probe failure")
            }
            let aPID = a.terminal.process.shellPid
            let bPID = b.terminal.process.shellPid
            guard aPID > 0, bPID > 0, aPID != bPID else { throw VisualEvidence.failure("No independent PTYs") }
            sendDockFixture("n\nblue\n", to: b)
            try await waitForFixture("Beta question") { self.receipt(bDirectory)?["answer"] as? String == "blue" }
            let prior = dashboard.snapshot?.snapshotAt ?? 0
            try await waitForFixture("actual automatic snapshot", seconds: 12) {
                (self.dashboard.snapshot?.snapshotAt ?? 0) > prior
            }
            dashboard.choose("codex-signed-out")
            try await VisualEvidence.settle()
            guard a.terminal.window == nil, b.terminal.window == nil, b.phase.isActive else {
                throw VisualEvidence.failure("Selecting an idle workspace ended a PTY or left it mounted")
            }

            // Restore the same normal management view to also test that a rebuilt
            // SwiftUI hierarchy cannot recreate or adopt either process.
            if let window {
                let frame = window.frame
                window.contentViewController = NSHostingController(rootView:
                    Management(model: dashboard, edgeEnabled: .constant(true)))
                window.setFrame(frame, display: true)
            }
            dashboard.choose(alpha.id)
            try await VisualEvidence.settle()
            guard a.terminal.window === window, a.terminal.process.shellPid == aPID,
                  b.terminal.process.shellPid == bPID, b.phase.isActive else {
                throw VisualEvidence.failure("Returning to Alpha did not reattach the same PTY")
            }
            sendDockFixture("amber\n", to: a)
            try await waitForFixture("Alpha question") { self.receipt(aDirectory)?["answer"] as? String == "amber" }
            try await VisualEvidence.settle()
            try captureManagementFixture(to: directory.appendingPathComponent("terminal-resumed.png"))
            guard a.terminal.findNext("Workspace ready", scrollToResult: false),
                  a.terminal.getSelection() == "Workspace ready" else {
                throw VisualEvidence.failure("Could not select terminal cells")
            }
            try await VisualEvidence.settle()
            try captureManagementFixture(to: directory.appendingPathComponent("terminal-selection.png"))
            a.terminal.selectNone()
            let original = try await measuredGrid(a, directory: aDirectory)
            guard let window else { throw VisualEvidence.failure("Missing fixture window") }
            let savedFrame = window.frame
            window.setContentSize(NSSize(width: 805, height: 560))
            try await VisualEvidence.settle()
            let resized = try await measuredGrid(a, directory: aDirectory)
            let cellHeight = Double(original[3]) / Double(original[0]) / window.backingScaleFactor
            guard a.terminal.font.pointSize == 13, (18...19).contains(cellHeight),
                  resized[0] < original[0], resized[1] < original[1], a.terminal.process.shellPid == aPID else {
                throw VisualEvidence.failure("Unexpected terminal size, font metrics or process change")
            }
            window.setFrame(savedFrame, display: true)
            VisualEvidence.frames.removeValue(forKey: "terminal-start")
            a.stop()
            try await waitForFixture("explicit Alpha stop and an idle workspace") {
                !a.phase.isActive && a.terminal.window == nil && VisualEvidence.frames["terminal-start"] != nil
            }
            try captureManagementFixture(to: directory.appendingPathComponent("terminal-stopped.png"))
            guard b.phase.isActive, b.terminal.process.shellPid == bPID else {
                throw VisualEvidence.failure("Beta changed after explicitly stopping Alpha: \(b.phase), PID \(b.terminal.process.shellPid)")
            }
            let rememberedDirectory = dashboard.terminalDirectory(for: alpha)
            let expectedDirectory = TerminalDirectory.validated(aDirectory, state: URL(fileURLWithPath: state, isDirectory: true))
            guard let expectedDirectory, rememberedDirectory == expectedDirectory else {
                throw VisualEvidence.failure("Alpha's remembered directory differed after stop: \(rememberedDirectory?.path ?? "none")")
            }
            b.stop()
            try await waitForFixture("explicit Beta stop") { !b.phase.isActive }
            let data = try JSONSerialization.data(withJSONObject: [
                "alpha": receipt(aDirectory) ?? [:], "beta": receipt(bDirectory) ?? [:],
                "alpha_wrapper_pid": aPID, "beta_wrapper_pid": bPID,
                "font_points": a.terminal.font.pointSize, "row_height_points": cellHeight,
                "normal_grid": original, "resized_grid": resized,
                "dark_application_light_workspace": true,
            ], options: [.prettyPrinted, .sortedKeys])
            try data.write(to: directory.appendingPathComponent("terminal-session-proof.json"))
            try Data("Two native PTYs preserved across switching and automatic refresh.\n".utf8)
                .write(to: directory.appendingPathComponent("dock-result"), options: .atomic)
        } catch {
            try? Data(error.localizedDescription.utf8).write(to: directory.appendingPathComponent("dock-result"), options: .atomic)
        }
        if await finishUserTerminals() { NSApp.terminate(nil) }
    }

    private func measuredGrid(_ session: UserTerminalSession, directory: URL) async throws -> [Int] {
        let size = session.terminal.getWindowSize()
        let expected = [size.ws_row, size.ws_col, size.ws_xpixel, size.ws_ypixel].map(Int.init)
        sendDockFixture("size\n", to: session)
        try await waitForFixture("PTY dimensions \(expected)") {
            self.receipt(directory)?["grid"] as? [Int] == expected
        }
        return expected
    }

    private func dockFixtureSession(_ instance: InstanceRecord, directory: URL) async throws -> UserTerminalSession {
        try await waitForFixture("docked \(instance.id)") {
            self.dashboard.terminalSession(for: instance)?.terminal.window === self.window && self.receipt(directory) != nil
        }
        guard let session = dashboard.terminalSession(for: instance), let record = receipt(directory),
              record["instance"] as? String == instance.id, record["directory"] as? String == directory.path,
              record["wrapper_pid"] as? Int32 == session.terminal.process.shellPid else {
            throw VisualEvidence.failure("Incorrect instance, directory or process binding")
        }
        return session
    }

    private func sendDockFixture(_ text: String, to session: UserTerminalSession) {
        precondition(session.terminal.window === window)
        session.terminal.send(data: Array(text.utf8)[...])
    }

    private func receipt(_ folder: URL) -> [String: Any]? {
        let file = folder.appendingPathComponent("fixture-dock-session.json")
        guard let data = try? Data(contentsOf: file) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private func captureStatusFixture(into directory: URL) async {
        do {
            let environment = ProcessInfo.processInfo.environment
            guard let state = environment["CLI_MANAGER_STATE"],
                  FileManager.default.fileExists(atPath: state + "/.status-ui-fixture") else {
                throw VisualEvidence.failure("Capture requires the isolated status fixture")
            }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            showManagement()
            try await VisualEvidence.settle()
            dashboard.choose("codex-query")
            window?.appearance = NSAppearance(named: .aqua)
            try await VisualEvidence.settle()
            let view = window?.contentView
            try VisualEvidence.capture(view?.superview ?? view, to: directory.appendingPathComponent("instance-status.png"))
            guard let instance = dashboard.instances.first(where: { $0.id == "codex-active" }) else {
                throw VisualEvidence.failure("Missing isolated terminal instance")
            }
            let folder = URL(fileURLWithPath: state).deletingLastPathComponent()
                .appendingPathComponent("Selected Fixture Workspace", isDirectory: true)
            dashboard.choose(instance.id)
            await dashboard.openUserTerminal(instance, directory: folder)
            try await VisualEvidence.settle()
            guard dashboard.terminalSession(for: instance)?.phase.isActive == true else {
                throw VisualEvidence.failure("Fixture terminal did not start")
            }
            try captureManagementFixture(to: directory.appendingPathComponent("user-terminal.png"))
            guard await finishUserTerminals() else { throw VisualEvidence.failure("Terminal did not exit") }
            try Data("fixture status and native PTY captured\n".utf8)
                .write(to: directory.appendingPathComponent("terminal-status-result"))
        } catch {
            try? Data(error.localizedDescription.utf8).write(to: directory.appendingPathComponent("terminal-status-result"))
        }
        NSApp.terminate(nil)
    }

    private func captureFixture(into directory: URL) async {
        do {
            let env = ProcessInfo.processInfo.environment
            guard let state = env["CLI_MANAGER_STATE"], URL(fileURLWithPath: state).lastPathComponent == "state",
                  FileManager.default.isExecutableFile(atPath: state + "/codex-fixture") else {
                throw VisualEvidence.failure("Capture requires the isolated UI fixture")
            }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try await VisualEvidence.settle()
            guard dashboard.instances.contains(where: { $0.id == "codex-studio" }) else {
                throw VisualEvidence.failure("The fixture snapshot failed to load")
            }
            dashboard.choose("codex-studio")
            showManagement()
            window?.appearance = NSAppearance(named: .aqua)
            try await VisualEvidence.settle()
            try VisualEvidence.capture(window?.contentView?.superview ?? window?.contentView,
                                       to: directory.appendingPathComponent("manager.png"))
            window?.appearance = NSAppearance(named: .darkAqua)
            try await VisualEvidence.settle()
            try VisualEvidence.capture(window?.contentView?.superview ?? window?.contentView,
                                       to: directory.appendingPathComponent("manager-dark.png"))
            try await edge.inspectFixture(into: directory)
            try Data("fixture UI smoke passed\n".utf8).write(to: directory.appendingPathComponent("result"))
        } catch {
            try? Data(error.localizedDescription.utf8).write(to: directory.appendingPathComponent("result"))
        }
        NSApp.terminate(nil)
    }
    #endif

    private func configureStatus() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "waveform.path", accessibilityDescription: "CLI Manager")
        item.button?.image?.isTemplate = true
        item.button?.toolTip = "CLI Manager"
        let menu = NSMenu()
        menu.addItem(action("管理实例…", #selector(showManagement), "o"))
        menu.addItem(action("显示本屏摘要", #selector(showPeek)))
        let toggle = action("屏幕顶边摘要", #selector(toggleEdge))
        toggle.state = edgeEnabled ? .on : .off
        menu.addItem(toggle)
        menu.addItem(.separator())
        menu.addItem(action("退出 CLI Manager", #selector(quit), "q"))
        item.menu = menu
        status = item
        statusEdgeToggle = toggle
    }

    private func configureMainMenu() {
        let menu = NSMenu()
        let root = NSMenuItem()
        let app = NSMenu(title: "CLI Manager")
        app.addItem(action("管理实例", #selector(showManagement), "o"))
        let peek = action("显示本屏摘要", #selector(showPeek), "p")
        peek.keyEquivalentModifierMask = [.command, .shift]
        app.addItem(peek)
        app.addItem(action("关闭窗口", #selector(closeManagement), "w"))
        app.addItem(.separator())
        app.addItem(action("退出 CLI Manager", #selector(quit), "q"))
        root.submenu = app
        menu.addItem(root)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        menu.addItem(editItem)
        NSApp.mainMenu = menu
    }

    private func action(_ title: String, _ selector: Selector, _ key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        item.target = self
        return item
    }

    @objc private func toggleEdge() { edgeEnabled.toggle() }

    @objc private func showPeek() { edge.openFromMenu() }

    @objc private func closeManagement() { (NSApp.keyWindow ?? window)?.performClose(nil) }
    @objc private func quit() { NSApp.terminate(nil) }

    @objc private func showManagement() {
        edge.hideSummaries()
        if window == nil {
            let screen = NSScreen.screens.first { $0.frame.contains(NSEvent.mouseLocation) } ?? NSScreen.main
            let rect = NSRect(x: 0, y: 0, width: 928, height: 685)
            let window = NSWindow(contentRect: rect, styleMask: [.titled, .closable, .miniaturizable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = "CLI Manager"
            window.appearance = NSAppearance(named: .aqua)
            window.backgroundColor = WorkspacePalette.native(WorkspacePalette.paper)
            window.contentMinSize = NSSize(width: 780, height: 540)
            window.tabbingMode = .disallowed
            window.isReleasedWhenClosed = false
            window.toolbarStyle = .unifiedCompact
            let toggle = Binding(get: { [weak self] in self?.edgeEnabled ?? true },
                                 set: { [weak self] enabled in self?.edgeEnabled = enabled })
            window.contentViewController = NSHostingController(rootView: Management(model: dashboard, edgeEnabled: toggle))
            if let frame = screen?.visibleFrame {
                window.setContentSize(NSSize(width: max(780, min(rect.width, frame.width - 30)),
                                             height: max(540, min(rect.height, frame.height - 50))))
                window.setFrameOrigin(NSPoint(x: frame.midX - window.frame.width / 2, y: frame.midY - window.frame.height / 2))
            } else {
                window.setContentSize(rect.size)
                window.center()
            }
            self.window = window
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
