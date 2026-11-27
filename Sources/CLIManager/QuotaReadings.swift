import SwiftUI

struct QuotaReading {
    let window: QuotaWindow?
    let provider: Provider
    let querying: Bool
    let failed: Bool
    let snapshotUnavailable: Bool

    var isExhausted: Bool {
        !snapshotUnavailable && window.map { !$0.expired && $0.remaining == 0 } == true
    }

    var remaining: Double? {
        if isExhausted { return 0 }
        guard !querying, !failed, !snapshotUnavailable, let window, !window.expired else { return nil }
        return window.remaining
    }

    var label: String { window?.shortLabel ?? "—" }

    var value: String {
        if let remaining { return remaining.formatted(.number.precision(.fractionLength(0...1))) + "%" }
        if querying { return "查询中" }
        if failed || snapshotUnavailable { return "未知" }
        switch window?.refreshReason {
        case "reset": return "待刷新"
        case "usage": return "待核实"
        default: return window?.expired == true ? "过期" : "无数据"
        }
    }

    var tint: Color {
        guard let remaining else { return .secondary }
        if isExhausted { return .red }
        return remaining < 20 ? .orange : .green
    }

    var resetDescription: String? {
        window?.resetsAt.map {
            (remaining == nil ? "上次记录的重置 " : "重置 ") + timestamp($0)
        }
    }

    private var descriptionOfPendingValue: String {
        if value == "待刷新" { return "额度窗口已重置，等待重新查询" }
        if value == "待核实" { return "此账户在观测后有 CLI 调用，剩余量待更新" }
        return value
    }

    var explanation: String {
        let source = provider == .codex ? "Codex account/rateLimits/read" : "Claude Code /usage"
        let status = isExhausted ? "额度耗尽，剩余 0%" : remaining != nil ? "剩余 \(value)" : descriptionOfPendingValue
        guard let window else {
            return "额度窗口 · \(status) · 来源 \(source)"
        }
        var parts = [window.name, status, "观测 \(timestamp(window.observedAt))"]
        if let resetDescription { parts.append(resetDescription) }
        return (parts + ["来源 \(source)"]).joined(separator: " · ")
    }

    static func split(_ windows: [QuotaWindow]) -> (slots: [QuotaWindow?], extras: [QuotaWindow]) {
        var rest = windows.sorted {
            // Prefer the provider's main bucket over model-specific buckets at equal duration.
            if $0.isProviderDefault != $1.isProviderDefault { return $0.isProviderDefault }
            if $0.expired != $1.expired { return !$0.expired }
            return $0.id < $1.id
        }
        func reserve(_ label: String) -> QuotaWindow? {
            guard let index = rest.firstIndex(where: { $0.shortLabel == label }) else { return nil }
            return rest.remove(at: index)
        }
        let short = reserve("5h")
        let long = reserve("7d")
        func nextOther() -> QuotaWindow? {
            guard let index = rest.firstIndex(where: { !["5h", "7d"].contains($0.shortLabel) }) else { return nil }
            return rest.remove(at: index)
        }
        return ([short ?? nextOther(), long ?? nextOther()], rest)
    }
}

extension QuotaWindow {
    var isProviderDefault: Bool { bucket == nil || ["codex", "claude"].contains(bucket ?? "") }

    var shortLabel: String {
        if let minutes, minutes > 0, minutes < 1_000_000, minutes.rounded() == minutes {
            let value = Int(minutes)
            if value.isMultiple(of: 1_440) { return "\(value / 1_440)d" }
            if value.isMultiple(of: 60) { return "\(value / 60)h" }
            return "\(value)m"
        }
        let label = window?.lowercased() ?? ""
        switch label {
        case "primary": return "主要"
        case "secondary": return "次要"
        case "session": return "会话"
        case "week": return "周"
        case "week (all models)": return "周·全部"
        default: return window ?? "额度"
        }
    }
}

struct MiniQuota: View {
    @Environment(\.colorScheme) private var scheme
    let reading: QuotaReading

    var body: some View {
        let color = legible(reading.tint, in: scheme)
        VStack(spacing: 3) {
            HStack(spacing: 2) {
                Text(reading.label)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 0)
                Text(reading.value)
                    .fontWeight(.semibold)
                    .monospacedDigit()
                    .foregroundStyle(color)
                    .frame(width: 42)
            }
            GeometryReader { geometry in
                Capsule()
                    .fill(reading.isExhausted ? color.opacity(0.34) : Color.primary.opacity(0.10))
                    .overlay(alignment: .leading) {
                        if let remaining = reading.remaining, remaining > 0 {
                            Capsule().fill(color.opacity(0.78)).frame(width: geometry.size.width * remaining / 100)
                        }
                    }
            }
            .frame(height: 2)
        }
        .font(.system(size: 10))
        .padding(.horizontal, 6)
        .padding(.vertical, 3)
        .frame(maxWidth: .infinity)
        .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 5))
        .help(reading.explanation)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(reading.explanation)
    }
}
