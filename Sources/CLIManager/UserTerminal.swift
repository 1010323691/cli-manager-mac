import AppKit
import Darwin
import SwiftTerm
import SwiftUI

enum TerminalDirectory {
    static func validated(_ candidate: URL, state: URL) -> URL? {
        guard candidate.isFileURL else { return nil }
        let directory = candidate.standardizedFileURL.resolvingSymlinksInPath()
        let registry = state.standardizedFileURL.resolvingSymlinksInPath()
        guard (try? directory.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true,
              !contains(registry, directory), !contains(directory, registry) else { return nil }
        return directory
    }

    private static func contains(_ parent: URL, _ child: URL) -> Bool {
        child.pathComponents.starts(with: parent.pathComponents)
    }
}

@MainActor
final class UserTerminalSession: Identifiable {
    enum Phase: Equatable {
        case preparing, active, stopping
        case ended(code: Int32?)
        case failed(String)

        var isActive: Bool {
            switch self {
            case .preparing, .active, .stopping: return true
            case .ended, .failed: return false
            }
        }

        var badge: BadgeData {
            switch self {
            case .preparing: BadgeData("启动中", "hourglass", .blue)
            case .active: BadgeData("会话运行中", "circle.fill", .green)
            case .stopping: BadgeData("结束中", "stop.circle", .orange)
            case .ended: BadgeData("会话已结束", "stop.circle", .secondary)
            case .failed: BadgeData("会话失败", "exclamationmark.circle", .orange)
            }
        }
    }

    nonisolated let id = UUID()
    let instanceID: String
    let directory: URL
    let terminal: LocalProcessTerminalView
    private let didChange: () -> Void
    private(set) var phase: Phase = .preparing { didSet { didChange() } }

    init(instanceID: String, directory: URL, didChange: @escaping () -> Void) {
        self.instanceID = instanceID
        self.directory = directory
        self.didChange = didChange
        terminal = LocalProcessTerminalView(frame: NSRect(x: 0, y: 0, width: 720, height: 540))
        terminal.terminalDelegate = self
        terminal.processDelegate = self
        WorkspacePalette.apply(to: terminal)
        terminal.setAccessibilityLabel("用户终端 \(instanceID)")
    }


    func start(_ launch: Runtime.ForegroundLaunch) {
        guard phase == .preparing else { return }
        terminal.startProcess(executable: launch.executable, args: launch.arguments,
                              environment: launch.environment, currentDirectory: directory.path)
        phase = terminal.process.running ? .active : .failed("终端进程未能启动。")
    }

    func failToStart(_ message: String) {
        if phase == .preparing { phase = .failed(message) }
    }

    func stop() {
        switch phase {
        case .preparing:
            phase = .ended(code: nil)
        case .active:
            let pid = terminal.process.shellPid
            guard pid > 0 else { return }
            // SwiftTerm monitors and reaps this child on the main queue. Keep the
            // PTY and session owned until it reports exit, including during stop.
            phase = .stopping
            if kill(pid, SIGTERM) != 0 && errno != ESRCH { phase = .active }
        case .stopping, .ended, .failed:
            break
        }
    }

    func focusIfMounted() {
        guard phase.isActive, let window = terminal.window, window.isKeyWindow, window.attachedSheet == nil else { return }
        window.makeFirstResponder(terminal)
    }
}

extension UserTerminalSession: @preconcurrency TerminalViewDelegate, @preconcurrency LocalProcessTerminalViewDelegate {
    func send(source: TerminalView, data: ArraySlice<UInt8>) {
        terminal.send(source: source, data: data)
    }

    func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
        terminal.sizeChanged(source: source, newCols: newCols, newRows: newRows)
    }

    func setTerminalTitle(source: TerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
    func scrolled(source: TerminalView, position: Double) {}
    func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {}
    func clipboardCopy(source: TerminalView, content: Data) {}
    func clipboardRead(source: TerminalView) -> Data? { nil }
    func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}

    func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}
    func setTerminalTitle(source: LocalProcessTerminalView, title: String) {}

    func processTerminated(source: TerminalView, exitCode: Int32?) {
        let code = exitCode.map { value in
            let signal = value & 0x7f
            return signal == 0 ? (value >> 8) & 0xff : 128 + signal
        }
        if phase == .stopping || code == 0 || code == nil {
            phase = .ended(code: code)
        } else {
            phase = .failed("会话已退出（退出码 \(code ?? 1)）。")
        }
    }
}

struct DockedTerminal: NSViewRepresentable {
    let session: UserTerminalSession

    func makeNSView(context: Context) -> TerminalHost {
        let host = TerminalHost()
        host.mount(session)
        return host
    }

    func updateNSView(_ host: TerminalHost, context: Context) { host.mount(session) }

    static func dismantleNSView(_ host: TerminalHost, coordinator: ()) { host.unmount() }
}

@MainActor
final class TerminalHost: NSView {
    private weak var session: UserTerminalSession?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = WorkspacePalette.native(WorkspacePalette.paper).cgColor
    }

    convenience init() { self.init(frame: .zero) }
    required init?(coder: NSCoder) { nil }

    func mount(_ target: UserTerminalSession) {
        guard session !== target || target.terminal.superview !== self else { return }
        unmount()
        session = target
        let terminal = target.terminal
        terminal.removeFromSuperview()
        // A transient zero-size SwiftUI mount must not reset a running PTY's rows.
        addSubview(terminal)
        requestTerminalFocus()
        needsLayout = true
    }

    func unmount() {
        guard let terminal = session?.terminal, terminal.superview === self else {
            session = nil
            return
        }
        if window?.firstResponder === terminal { window?.makeFirstResponder(nil) }
        terminal.removeFromSuperview()
        session = nil
    }

    override func layout() {
        super.layout()
        let inset = bounds.insetBy(dx: 18, dy: 14)
        if inset.width > 0, inset.height > 0, session?.terminal.superview === self {
            session?.terminal.frame = inset
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        requestTerminalFocus()
    }

    private func requestTerminalFocus() {
        DispatchQueue.main.async { [weak self] in
            guard let self, let session = self.session, session.terminal.superview === self else { return }
            session.focusIfMounted()
        }
    }
}
