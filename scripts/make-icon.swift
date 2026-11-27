import AppKit

let side: CGFloat = 1024
let bounds = NSRect(x: 0, y: 0, width: side, height: side)
let iconset = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let image = NSImage(size: bounds.size, flipped: false) { _ in
    let disk = NSBezierPath(roundedRect: bounds.insetBy(dx: 91, dy: 91), xRadius: 194, yRadius: 194)
    NSGradient(starting: NSColor(srgbRed: 0.19, green: 0.30, blue: 0.46, alpha: 1),
               ending: NSColor(srgbRed: 0.045, green: 0.075, blue: 0.13, alpha: 1))!
        .draw(in: disk, angle: -80)
    NSColor.white.withAlphaComponent(0.27).setStroke()
    disk.lineWidth = 6
    disk.stroke()

    let rail = NSBezierPath()
    rail.move(to: CGPoint(x: 204, y: 512))
    let points: [(CGFloat, CGFloat)] = [
        (311, 512), (361, 600), (421, 412), (496, 714), (569, 336), (643, 599),
        (704, 470), (754, 512), (820, 512),
    ]
    for (x, y) in points { rail.line(to: CGPoint(x: x, y: y)) }
    rail.lineWidth = 32
    rail.lineCapStyle = .round
    rail.lineJoinStyle = .round
    NSColor(srgbRed: 0.53, green: 0.89, blue: 0.98, alpha: 1).setStroke()
    rail.stroke()
    return true
}

func write(_ name: String, pixels: Int) throws {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                  colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    NSGraphicsContext.current?.imageInterpolation = .high
    image.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels))
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using: .png, properties: [:])!.write(to: iconset.appendingPathComponent(name))
}

for size in [16, 32, 128, 256, 512] {
    try write("icon_\(size)x\(size).png", pixels: size)
    try write("icon_\(size)x\(size)@2x.png", pixels: size * 2)
}
