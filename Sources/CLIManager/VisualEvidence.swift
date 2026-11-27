#if DEBUG
import AppKit
import SwiftUI

@MainActor
enum VisualEvidence {
    static var frames: [String: CGRect] = [:]
    static func capture(_ view: NSView?, to file: URL, backdrop: CGColor? = nil) throws {
        guard let view else { throw failure("Missing AppKit window content") }
        view.layoutSubtreeIfNeeded()
        view.displayIfNeeded()
        let bounds = view.bounds
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: bounds) else {
            throw failure("No bitmap for AppKit window content")
        }
        view.cacheDisplay(in: bounds, to: bitmap)
        let output: NSBitmapImageRep
        if let backdrop, let source = bitmap.cgImage,
           let context = CGContext(data: nil, width: source.width, height: source.height, bitsPerComponent: 8,
                                   bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                   bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) {
            let frame = CGRect(x: 0, y: 0, width: source.width, height: source.height)
            context.setFillColor(backdrop)
            context.fill(frame)
            context.draw(source, in: frame)
            guard let composited = context.makeImage() else { throw failure("Could not composite AppKit crop") }
            output = NSBitmapImageRep(cgImage: composited)
        } else {
            output = bitmap
        }
        guard let png = output.representation(using: .png, properties: [:]) else {
            throw failure("Could not encode AppKit window content")
        }
        try png.write(to: file, options: .atomic)
    }

    static func failure(_ reason: String) -> NSError {
        NSError(domain: "CLIManager.UISmoke", code: 1, userInfo: [NSLocalizedDescriptionKey: reason])
    }

    static func settle() async throws {
        try await Task.sleep(for: .milliseconds(560))
    }
}
#endif
