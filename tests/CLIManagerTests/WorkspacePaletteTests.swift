import XCTest
@testable import CLIManager

final class WorkspacePaletteTests: XCTestCase {
    func testAnsiAndDefaultInkAreLegibleOnPaperAndSelection() {
        let ansi = WorkspacePalette.ansi
        XCTAssertEqual(ansi.count, 16)
        XCTAssertEqual(Set(ansi).count, 16)
        for value in ansi + [WorkspacePalette.ink, WorkspacePalette.muted, WorkspacePalette.accent] {
            for surface in [WorkspacePalette.paper, WorkspacePalette.selection] {
                let ratio = (luminance(surface) + 0.05) / (luminance(value) + 0.05)
                XCTAssertGreaterThanOrEqual(ratio, 4.5, String(format: "#%06X", value))
            }
        }
    }

    private func luminance(_ rgb: UInt32) -> Double {
        let components = [16, 8, 0].map { Double((rgb >> $0) & 255) / 255 }
        let linear = components.map { $0 <= 0.04045 ? $0 / 12.92 : pow(($0 + 0.055) / 1.055, 2.4) }
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
    }
}
