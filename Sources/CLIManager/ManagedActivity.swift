import SwiftUI

struct ManagedOutput: Decodable, Equatable, Sendable {
    let schema: Int
    let attemptID: String
    let state: String
    let entries: [Entry]

    enum CodingKeys: String, CodingKey {
        case schema, state, entries
        case attemptID = "attempt_id"
    }

    struct Entry: Decodable, Equatable, Identifiable, Sendable {
        let id: String
        let kind: String
        let text: String
    }
}

@MainActor
struct ManagedActivityPane: View {
    let model: Dashboard
    let attempts: [LiveAttempt]
    @State private var selection: String?
    @State private var output: ManagedOutput?
    @State private var failedAttempt: String?

    private var selected: LiveAttempt? {
        attempts.first { $0.id == selection } ?? attempts.first
    }

    var body: some View {
        if let attempt = selected {
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    if attempts.count > 1 {
                        Picker("托管任务", selection: Binding(get: { attempt.id }, set: { selection = $0 })) {
                            ForEach(attempts) { Text($0.taskID).tag($0.id) }
                        }
                        .labelsHidden()
                        .accessibilityLabel("切换托管任务")
                    } else {
                        Text(attempt.taskID)
                            .font(.system(.caption, design: .monospaced).weight(.semibold))
                            .lineLimit(1)
                    }
                    Spacer()
                    StateBadge(data: attempt.badge)
                }
                .padding(.horizontal, 14)
                .frame(height: 39)
                .overlay(alignment: .bottom) {
                    Rectangle().fill(WorkspacePalette.color(WorkspacePalette.rule)).frame(height: 1)
                }
                outputView(attempt)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .foregroundStyle(WorkspacePalette.color(WorkspacePalette.ink))
            .background(WorkspacePalette.color(WorkspacePalette.paper))
            .task(id: attempt.id) { await watch(attempt) }
        }
    }

    @ViewBuilder
    private func outputView(_ attempt: LiveAttempt) -> some View {
        let current = output.flatMap { $0.attemptID == attempt.id ? $0 : nil }
        if let current, !current.entries.isEmpty, failedAttempt != attempt.id {
            GeometryReader { viewport in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(current.entries) { entry in
                            if entry.kind == "operation" {
                                Label(entry.text, systemImage: "chevron.right")
                                    .foregroundStyle(WorkspacePalette.color(WorkspacePalette.muted))
                            } else {
                                Text(entry.text)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    .font(.system(size: 13, design: .monospaced))
                    .lineSpacing(4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(20)
                    .frame(minHeight: viewport.size.height, alignment: .topLeading)
                }
                .defaultScrollAnchor(.bottom)
            }
            .id(attempt.id)
            .accessibilityIdentifier("managed-output")
            .measureFixture("managed-output")
        } else {
            let unavailable = failedAttempt == attempt.id || current?.state == "unavailable"
            let message = unavailable ? "无法读取当前任务输出"
                : current?.state == "ended" ? "此任务已结束" : "正在等待任务输出"
            Label(message, systemImage: unavailable ? "exclamationmark.circle" : "text.alignleft")
                .font(.subheadline)
                .foregroundStyle(WorkspacePalette.color(WorkspacePalette.muted))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func watch(_ attempt: LiveAttempt) async {
        output = nil
        failedAttempt = nil
        while !Task.isCancelled {
            do {
                let next = try await model.managedOutput(for: attempt)
                guard !Task.isCancelled else { return }
                output = next
                failedAttempt = nil
                if next.state == "ended" {
                    await model.refresh()
                    return
                }
            } catch {
                guard !Task.isCancelled else { return }
                failedAttempt = attempt.id
            }
            try? await Task.sleep(for: .milliseconds(1500))
        }
    }
}
