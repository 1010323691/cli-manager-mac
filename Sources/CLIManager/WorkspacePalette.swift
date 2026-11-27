import AppKit
import CoreText
import SwiftTerm
import SwiftUI

enum WorkspacePalette {
    // Near-white paper, cooler controls, and ink colors tuned for both the
    // terminal paper and its blue selection.
    static let paper: UInt32 = 0xFCFDFE
    static let sidebar: UInt32 = 0xF1F4F8
    static let row: UInt32 = 0xF7F9FC
    static let toolbar: UInt32 = 0xF7F9FC
    static let rule: UInt32 = 0xDCE4EE
    static let ink: UInt32 = 0x1D293A
    static let muted: UInt32 = 0x526174
    static let accent: UInt32 = 0x255AA6
    static let selection: UInt32 = 0xDEEBFC
    static let selectionBorder: UInt32 = 0xB6CDEE

    // SGR 30–37 followed by 90–97. White is a cool neutral ink on light paper.
    static let ansi: [UInt32] = [
        0x263346, 0x9B283D, 0x205E42, 0x71501E, 0x254C91, 0x694181, 0x145662, 0x46576B,
        0x566579, 0xA92D46, 0x216845, 0x765006, 0x2256A7, 0x79448D, 0x15616C, 0x354457,
    ]

    static func color(_ value: UInt32) -> SwiftUI.Color { SwiftUI.Color(nsColor: native(value)) }

    static func native(_ value: UInt32) -> NSColor {
        let (red, green, blue) = components(value)
        return NSColor(srgbRed: CGFloat(red) / 255, green: CGFloat(green) / 255,
                       blue: CGFloat(blue) / 255, alpha: 1)
    }

    private static func components(_ value: UInt32) -> (UInt16, UInt16, UInt16) {
        (UInt16((value >> 16) & 255), UInt16((value >> 8) & 255), UInt16(value & 255))
    }

    @MainActor
    static func apply(to terminal: LocalProcessTerminalView) {
        let font = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
        terminal.font = font
        let metrics = CTFontGetAscent(font) + CTFontGetDescent(font) + CTFontGetLeading(font)
        terminal.lineSpacing = 18.5 / metrics
        terminal.nativeForegroundColor = native(ink)
        terminal.nativeBackgroundColor = native(paper)
        terminal.caretColor = native(accent)
        terminal.caretTextColor = native(paper)
        terminal.selectedTextBackgroundColor = native(selection)
        terminal.selectedTextForegroundColor = native(ink)
        terminal.installColors(ansi.map { value in
            let (red, green, blue) = components(value)
            return SwiftTerm.Color(red8: red, green8: green, blue8: blue)
        })
    }
}
