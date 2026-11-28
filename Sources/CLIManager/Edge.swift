import AppKit
import Combine
import SwiftUI

@MainActor
private final class PeekState: ObservableObject {
    @Published var isOpen = false
    @Published var isPinned = false
}

@MainActor
private struct PeekView: View {
    @ObservedObject var dashboard: Dashboard
    @ObservedObject var state: PeekState
    @Environment(\.accessibilityReduceMotion) private var lessMotion
    @Environment(\.accessibilityReduceTransparency) private var opaque
    @Environment(\.colorSchemeContrast) private var contrast
    var topInset: CGFloat
    let close: () -> Void

    private let shape = UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: 22,
                                               bottomTrailingRadius: 22, topTrailingRadius: 0)
    private let paper = WorkspacePalette.color(WorkspacePalette.paper)
    private let ink = WorkspacePalette.color(WorkspacePalette.ink)
    private let muted = WorkspacePalette.color(WorkspacePalette.muted)
    private let accent = WorkspacePalette.color(WorkspacePalette.accent)
    private let selection = WorkspacePalette.color(WorkspacePalette.selection)

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .top) {
                Capsule()
                    .fill(.white.opacity(opaque ? 1 : 0.90))
                    .overlay { Capsule().strokeBorder(muted.opacity(0.58), lineWidth: 0.75) }
                    .frame(width: 49, height: 4)
                    .padding(.top, 2)
                    .opacity(state.isOpen ? 0 : 1)
                    .accessibilityHidden(true)
                pane
                    .frame(width: geometry.size.width, height: geometry.size.height, alignment: .top)
                    .offset(y: state.isOpen || lessMotion ? 0 : -geometry.size.height + 5)
                    .opacity(state.isOpen ? 1 : 0)
                    .allowsHitTesting(state.isOpen)
                    .accessibilityHidden(!state.isOpen)
            }
            .animation(lessMotion ? nil : .spring(response: 0.34, dampingFraction: 0.88), value: state.isOpen)
        }
        .clipped()
        .environment(\.colorScheme, .light)
    }

    private var pane: some View {
        let attempts = dashboard.liveAttempts
        return VStack(alignment: .leading, spacing: EdgeGeometry.contentSpacing) {
            HStack(spacing: 8) {
                Image(systemName: "waveform.path")
                    .foregroundStyle(accent)
                    .font(.system(size: 14, weight: .semibold))
                    .frame(width: 28, height: 28)
                    .background(selection.opacity(0.85), in: RoundedRectangle(cornerRadius: 7))
                    .accessibilityHidden(true)
                Text("任务活动")
                    .font(.system(size: 15, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 2)
                if attempts.count > EdgeGeometry.visibleActivityRows {
                    Text("\(attempts.count) 项")
                        .font(.caption.weight(.medium).monospacedDigit())
                        .foregroundStyle(muted)
                        .accessibilityLabel("\(attempts.count) 个活动任务")
                }
                toolButton(state.isPinned ? "pin.fill" : "pin", state.isPinned ? "取消固定" : "固定摘要",
                           selected: state.isPinned) {
                    state.isPinned.toggle()
                }
                .keyboardShortcut("p", modifiers: [])
                toolButton("xmark", "收起摘要", action: close)
            }
            .frame(height: EdgeGeometry.headerHeight)
            Group {
                if attempts.count > EdgeGeometry.visibleActivityRows {
                    ScrollView {
                        activityRows(attempts)
                    }
                    .accessibilityIdentifier("edge-activity-list")
                } else if !attempts.isEmpty {
                    activityRows(attempts)
                    .frame(maxHeight: .infinity, alignment: .top)
                } else if dashboard.snapshotError != nil {
                    activityPlaceholder("本地状态读取失败", icon: "exclamationmark.triangle", color: legible(.orange, in: .light))
                } else if dashboard.snapshot == nil {
                    activityPlaceholder("正在读取本地状态", icon: "ellipsis", color: muted)
                } else {
                    activityPlaceholder("暂无运行中的任务", icon: "tray", color: muted)
                }
            }
            .frame(height: EdgeGeometry.activityHeight)
        }
        .padding(.horizontal, 16)
        .padding(.top, topInset)
        .padding(.bottom, EdgeGeometry.bottomInset)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .foregroundStyle(ink, muted)
        .modifier(PeekMaterial(shape: shape, paper: paper, opaque: opaque))
        .overlay {
            let edge = muted.opacity(contrast == .increased ? 0.56 : 0.30)
            shape.strokeBorder(.linearGradient(colors: [.white.opacity(0.95), edge],
                                               startPoint: .top, endPoint: .bottom), lineWidth: 1)
                .allowsHitTesting(false)
        }
    }

    private func activityRows(_ attempts: [LiveAttempt]) -> some View {
        LazyVStack(spacing: EdgeGeometry.activityRowSpacing) {
            ForEach(attempts) { activityRow($0) }
        }
    }

    private func activityRow(_ attempt: LiveAttempt) -> some View {
        let badge = attempt.badge
        let statusInk = legible(badge.color, in: .light)
        return HStack(spacing: 7) {
            Image(systemName: badge.icon)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(statusInk)
                .frame(width: 17)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(attempt.taskID).font(.system(size: 12, weight: .semibold, design: .monospaced))
                Text(attempt.instance).font(.system(size: 10)).foregroundStyle(muted)
            }
            .lineLimit(1)
            Spacer(minLength: 3)
            Text(badge.title)
                .font(.caption2.weight(.medium))
                .foregroundStyle(statusInk)
                .fixedSize()
        }
        .padding(.horizontal, 8)
        .frame(height: EdgeGeometry.activityRowHeight)
        .background(ink.opacity(0.055), in: RoundedRectangle(cornerRadius: 7))
        .help("\(attempt.taskID) · \(attempt.instance) · \(badge.title)")
        .accessibilityElement(children: .combine)
    }

    private func activityPlaceholder(_ title: String, icon: String, color: Color) -> some View {
        Label(title, systemImage: icon)
            .font(.subheadline)
            .foregroundStyle(color)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func toolButton(_ symbol: String, _ title: String, selected: Bool = false,
                            action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .semibold))
                .frame(width: 25, height: 25)
                .foregroundStyle(selected ? accent : muted)
                .background(selected ? selection : ink.opacity(0.055), in: RoundedRectangle(cornerRadius: 7))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(title)
        .accessibilityLabel(title)
    }

}

private struct PeekMaterial: ViewModifier {
    let shape: UnevenRoundedRectangle
    let paper: Color
    let opaque: Bool

    func body(content: Content) -> some View {
        if opaque {
            content.background(paper, in: shape)
        } else if #available(macOS 26, *) {
            content.background {
                shape.fill(.linearGradient(colors: [.white.opacity(0.90), paper.opacity(0.83)],
                                           startPoint: .top, endPoint: .bottom))
                    .glassEffect(.regular, in: shape)
                    .allowsHitTesting(false)
            }
        } else {
            content.background {
                shape.fill(.regularMaterial).overlay { shape.fill(paper.opacity(0.84)) }
            }
        }
    }
}

private final class PeekHostingView: NSHostingView<PeekView> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

private final class PeekPanel: NSPanel {
    var keyboardAccess = false
    var dismiss: (() -> Void)?
    override var canBecomeKey: Bool { keyboardAccess }
    override var canBecomeMain: Bool { false }

    override func cancelOperation(_ sender: Any?) { dismiss?() }
}

@MainActor
final class EdgeController {
    @MainActor
    private final class ScreenPeek {
        let panel: PeekPanel
        let state: PeekState
        let geometry: EdgeGeometry
        var hover = EdgeHover()

        init(panel: PeekPanel, state: PeekState, geometry: EdgeGeometry) {
            self.panel = panel
            self.state = state
            self.geometry = geometry
        }

        func reveal(keyboard: Bool = false) {
            state.isOpen = true
            panel.ignoresMouseEvents = false
            panel.keyboardAccess = keyboard
            if keyboard {
                state.isPinned = true
                panel.makeKeyAndOrderFront(nil)
            } else { panel.orderFrontRegardless() }
        }

        func collapse() {
            state.isPinned = false
            state.isOpen = false
            panel.ignoresMouseEvents = true
            if panel.isKeyWindow { panel.orderOut(nil) }
            panel.keyboardAccess = false
            hover.collapsed()
        }
    }

    private let model: Dashboard
    private let onManage: () -> Void
    private var windows: [CGDirectDisplayID: ScreenPeek] = [:]
    private var timer: Timer?
    private var snapshotObserver: AnyCancellable?
    private var previousAttemptIDs: Set<String> = []
    // Hold the request while all displays temporarily hide their menu bars.
    private var holdsActivity = false
    private var activityDisplay: CGDirectDisplayID?
    var enabled = true {
        didSet {
            guard enabled != oldValue else { return }
            if !enabled {
                holdsActivity = false
                activityDisplay = nil
            }
            rebuild()
        }
    }

    init(model: Dashboard, onManage: @escaping () -> Void) {
        self.model = model
        self.onManage = onManage
    }

    func start() {
        rebuild()
        snapshotObserver = model.$snapshot.sink { [weak self] snapshot in
            self?.observeActivity(snapshot)
        }
        let timer = Timer(timeInterval: 0.055, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        snapshotObserver = nil
        windows.values.forEach { $0.panel.orderOut(nil) }
        windows.removeAll()
    }

    func rebuild() {
        windows.values.forEach { $0.panel.orderOut(nil) }
        windows.removeAll()
        for screen in NSScreen.screens {
            guard let id = screen.displayID else { continue }
            let geometry = layout(for: screen)
            let rect = geometry.panel
            let panel = PeekPanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel],
                                  backing: .buffered, defer: false, screen: nil)
            panel.setFrame(rect, display: false)
            panel.identifier = NSUserInterfaceItemIdentifier("CLIManagerPeek")
            panel.title = "CLI Manager · 摘要"
            panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenNone]
            panel.hidesOnDeactivate = false
            panel.isOpaque = false
            panel.appearance = NSAppearance(named: .aqua)
            panel.backgroundColor = .clear
            panel.hasShadow = false
            panel.animationBehavior = .none
            panel.ignoresMouseEvents = true
            panel.isFloatingPanel = true
            panel.level = .statusBar
            let state = PeekState()
            let peek = ScreenPeek(panel: panel, state: state, geometry: geometry)
            let dismiss = { [weak self] in self?.dismiss(id) ?? () }
            let content = PeekView(dashboard: model, state: state, topInset: geometry.contentTopInset,
                                   close: dismiss)
            let hosting = PeekHostingView(rootView: content)
            panel.contentView = hosting
            panel.initialFirstResponder = hosting
            panel.becomesKeyOnlyIfNeeded = false
            panel.dismiss = dismiss
            windows[id] = peek
            if enabled && geometry.menuBarVisible { panel.orderFrontRegardless() }
        }
    }

    func spaceChanged() {
        for peek in windows.values { peek.collapse() }
        poll()
    }

    func hideSummaries() {
        for (id, peek) in windows where peek.state.isOpen && !peek.state.isPinned && !holdsActivity(on: id) {
            peek.collapse()
        }
    }

    func openFromMenu() {
        let location = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { $0.frame.contains(location) } ?? NSScreen.main
        guard let screen, layout(for: screen).menuBarVisible, let id = screen.displayID, let peek = windows[id] else {
            onManage()
            return
        }
        let otherWindows = windows.values.filter { $0.panel != peek.panel && $0.panel.keyboardAccess }
        otherWindows.forEach { $0.collapse() }
        peek.reveal(keyboard: true)
    }

    private func observeActivity(_ snapshot: Snapshot?) {
        guard let snapshot else { return }
        let attempts = LiveAttempt.normalized(snapshot.liveAttempts ?? [])
        let ids = Set(attempts.map(\.attemptID))
        let arrived = !ids.isSubset(of: previousAttemptIDs)
        previousAttemptIDs = ids
        if ids.isEmpty {
            let peek = activityDisplay.flatMap { windows[$0] }
            holdsActivity = false
            activityDisplay = nil
            if let peek, !peek.state.isPinned { peek.collapse() }
        } else if arrived && enabled {
            holdsActivity = true
            poll()
        }
    }

    private func holdsActivity(on id: CGDirectDisplayID) -> Bool {
        holdsActivity && activityDisplay == id
    }

    private func dismiss(_ id: CGDirectDisplayID) {
        if holdsActivity(on: id) {
            holdsActivity = false
            activityDisplay = nil
        }
        windows[id]?.collapse()
    }

    private func layout(for screen: NSScreen) -> EdgeGeometry {
        EdgeGeometry(screen: screen.frame, visible: screen.visibleFrame, safeTop: screen.safeAreaInsets.top)
    }

    private func poll(
        mouse: CGPoint = NSEvent.mouseLocation, time: TimeInterval = ProcessInfo.processInfo.systemUptime,
        pointerDown: Bool = NSEvent.pressedMouseButtons != 0
    ) {
        let screens = NSScreen.screens
        let available = screens.filter { layout(for: $0).menuBarVisible }
        let displayIDs = Set(available.compactMap(\.displayID))
        if holdsActivity {
            if activityDisplay.map(displayIDs.contains) != true {
                activityDisplay = (available.first { $0.frame.contains(mouse) } ?? available.first)?.displayID
            }
            if let activityDisplay, let peek = windows[activityDisplay], !peek.state.isOpen {
                peek.reveal()
            }
        }
        for screen in screens {
            guard let id = screen.displayID, let peek = windows[id] else { continue }
            guard displayIDs.contains(id), enabled || peek.state.isOpen else {
                if peek.panel.isVisible {
                    peek.collapse()
                    peek.panel.orderOut(nil)
                }
                continue
            }
            if !peek.panel.isVisible { peek.panel.orderFrontRegardless() }
            let decision = peek.hover.sample(
                atTrigger: enabled && peek.geometry.hitsTrigger(mouse),
                withinPanel: peek.panel.frame.insetBy(dx: -11, dy: -7).contains(mouse),
                pointerDown: pointerDown,
                isOpen: peek.state.isOpen, isPinned: peek.state.isPinned || holdsActivity(on: id), now: time
            )
            if decision == .open { peek.reveal() }
            if decision == .close { peek.collapse() }
        }
    }
    #if DEBUG
    func verifyAutomaticActivityFixture(isHeld: Bool) throws {
        guard isHeld else {
            guard !holdsActivity, windows.values.allSatisfy({ !$0.state.isOpen }) else {
                throw VisualEvidence.failure("Activity remained open without an automatic reveal")
            }
            return
        }
        guard holdsActivity, let activityDisplay, let peek = windows[activityDisplay],
              peek.state.isOpen, !peek.panel.isKeyWindow else {
            throw VisualEvidence.failure("Activity did not open without taking keyboard focus")
        }
        let outside = CGPoint(x: peek.geometry.panel.midX, y: peek.geometry.panel.minY - 30)
        let time = ProcessInfo.processInfo.systemUptime
        poll(mouse: outside, time: time, pointerDown: false)
        poll(mouse: outside, time: time + 0.6, pointerDown: false)
        hideSummaries()
        guard peek.state.isOpen else { throw VisualEvidence.failure("Activity did not stay open") }
    }

    func dismissActivityFixture() {
        if let id = activityDisplay ?? NSScreen.main?.displayID { windows[id]?.panel.dismiss?() }
    }

    func captureActivityFixture(to file: URL) async throws -> CGRect {
        timer?.invalidate()
        let screen = NSScreen.screens.first { $0.displayID == activityDisplay } ?? NSScreen.main
        guard let screen, let id = screen.displayID,
              let peek = windows[id], layout(for: screen).menuBarVisible else {
            throw VisualEvidence.failure("No display with a visible menu bar")
        }
        if !peek.state.isOpen {
            let point = CGPoint(x: peek.geometry.trigger.midX, y: peek.geometry.trigger.midY)
            let away = CGPoint(x: point.x, y: peek.geometry.panel.minY - 30)
            let time = ProcessInfo.processInfo.systemUptime
            poll(mouse: away, time: time, pointerDown: false)
            poll(mouse: point, time: time, pointerDown: false)
            poll(mouse: point, time: time + 0.3, pointerDown: false)
        }
        guard peek.state.isOpen else { throw VisualEvidence.failure("Activity panel did not open") }
        try await VisualEvidence.settle()
        // The window server composites the glass against other windows. Export this
        // panel by itself over graphite so the crop contains no desktop content.
        let backdrop = NSColor(srgbRed: 0.125, green: 0.15, blue: 0.21, alpha: 1)
        try VisualEvidence.capture(peek.panel.contentView, to: file, backdrop: backdrop.cgColor)
        return peek.panel.frame
    }

    func inspectFixture(into directory: URL) async throws {
        timer?.invalidate()
        guard let screen = NSScreen.main, let id = screen.displayID, let peek = windows[id],
              enabled, layout(for: screen).menuBarVisible else {
            throw VisualEvidence.failure("No display with a visible menu bar")
        }
        let edgePoint = CGPoint(x: peek.geometry.trigger.midX, y: peek.geometry.trigger.midY)
        let away = CGPoint(x: peek.geometry.panel.midX, y: peek.geometry.panel.minY - 30)
        let time = ProcessInfo.processInfo.systemUptime
        poll(mouse: edgePoint, time: time, pointerDown: false)
        guard !peek.state.isOpen else { throw VisualEvidence.failure("Peek opened before dwell time") }
        poll(mouse: edgePoint, time: time + 0.3, pointerDown: false)
        guard peek.state.isOpen else { throw VisualEvidence.failure("Peek failed to open") }
        try await VisualEvidence.settle()
        try VisualEvidence.capture(peek.panel.contentView, to: directory.appendingPathComponent("top-edge.png"))
        poll(mouse: away, time: time + 1, pointerDown: false)
        poll(mouse: away, time: time + 1.5, pointerDown: false)
        guard !peek.state.isOpen else { throw VisualEvidence.failure("Peek failed to retract") }
        try await VisualEvidence.settle()

        enabled = false
        guard let manual = windows[id] else { throw VisualEvidence.failure("Manual peek unavailable") }
        manual.reveal(keyboard: true)
        try await VisualEvidence.settle()
        guard manual.state.isOpen && manual.state.isPinned && manual.panel.isKeyWindow else {
            throw VisualEvidence.failure("Manual peek failed to receive keyboard focus")
        }
        poll(mouse: away, time: time + 3, pointerDown: false)
        poll(mouse: away, time: time + 3.5, pointerDown: false)
        guard manual.state.isOpen else { throw VisualEvidence.failure("Pinned peek retracted") }
        manual.state.isPinned = false
        poll(mouse: away, time: time + 4, pointerDown: false)
        poll(mouse: away, time: time + 4.5, pointerDown: false)
        guard !manual.state.isOpen && !enabled else { throw VisualEvidence.failure("Unpin changed hover preference") }
        enabled = true
    }
    #endif
}

extension NSScreen {
    var displayID: CGDirectDisplayID? {
        (deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
    }
}
