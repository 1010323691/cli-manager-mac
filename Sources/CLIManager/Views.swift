import SwiftUI

func legible(_ color: Color, in scheme: ColorScheme) -> Color {
    guard scheme == .light else { return color }
    switch color {
    case .green: return Color(red: 0.07, green: 0.38, blue: 0.25)
    case .orange: return Color(red: 0.54, green: 0.25, blue: 0.035)
    case .blue: return Color(red: 0.07, green: 0.31, blue: 0.64)
    case .red: return Color(red: 0.63, green: 0.16, blue: 0.18)
    default: return color
    }
}

struct StateBadge: View {
    @Environment(\.colorScheme) private var scheme
    let data: BadgeData

    var body: some View {
        Label(data.title, systemImage: data.icon)
            .font(.caption.weight(.semibold))
            .lineLimit(1)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .foregroundStyle(legible(data.color, in: scheme))
            .background(data.color.opacity(0.11), in: Capsule())
            .accessibilityLabel(data.title)
    }
}

struct ProviderTile: View {
    let provider: Provider
    var size: CGFloat = 32

    var body: some View {
        Image(systemName: provider.symbol)
            .font(.system(size: size * 0.43, weight: .semibold))
            .foregroundStyle(WorkspacePalette.color(provider == .codex ? WorkspacePalette.accent : 0x8F5132))
            .frame(width: size, height: size)
            .background(WorkspacePalette.color(provider == .codex ? WorkspacePalette.selection : 0xF5E8E0),
                        in: RoundedRectangle(cornerRadius: size * 0.28))
            .accessibilityHidden(true)
    }
}

struct SectionLabel: View {
    let title: String
    let symbol: String

    var body: some View {
        Label(title, systemImage: symbol)
            .font(.headline)
            .accessibilityAddTraits(.isHeader)
    }
}

func shortTime(_ stamp: Double) -> String {
    let date = Date(timeIntervalSince1970: stamp)
    let format: Date.FormatStyle.DateStyle = Calendar.current.isDateInToday(date) ? .omitted : .numeric
    return date.formatted(date: format, time: .shortened)
}

struct EmptyState: View {
    let title: String
    let text: String
    let symbol: String

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(.secondary)
            Text(title).font(.headline)
            Text(text).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
