import AppKit

guard CommandLine.arguments.count == 3 else {
    fputs("usage: make_icon VERSION OUTPUT.png\n", stderr)
    exit(2)
}

let version = CommandLine.arguments[1]
let output = CommandLine.arguments[2]
let size = 1024

guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: size,
    pixelsHigh: size,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
) else { exit(1) }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSGraphicsContext.current?.imageInterpolation = .high

let canvas = NSRect(x: 0, y: 0, width: size, height: size)
NSColor.clear.setFill()
canvas.fill()

let tile = NSBezierPath(roundedRect: NSRect(x: 72, y: 72, width: 880, height: 880), xRadius: 205, yRadius: 205)
let gradient = NSGradient(colors: [
    NSColor(calibratedRed: 0.08, green: 0.31, blue: 0.64, alpha: 1),
    NSColor(calibratedRed: 0.05, green: 0.12, blue: 0.27, alpha: 1)
])!
gradient.draw(in: tile, angle: -55)

NSGraphicsContext.saveGraphicsState()
tile.addClip()
NSColor(calibratedWhite: 1, alpha: 0.10).setStroke()
for position in stride(from: 150, through: 900, by: 94) {
    let vertical = NSBezierPath()
    vertical.move(to: NSPoint(x: position, y: 95))
    vertical.line(to: NSPoint(x: position, y: 929))
    vertical.lineWidth = 4
    vertical.stroke()

    let horizontal = NSBezierPath()
    horizontal.move(to: NSPoint(x: 95, y: position))
    horizontal.line(to: NSPoint(x: 929, y: position))
    horizontal.lineWidth = 4
    horizontal.stroke()
}
NSGraphicsContext.restoreGraphicsState()

NSColor(calibratedWhite: 1, alpha: 0.26).setStroke()
tile.lineWidth = 10
tile.stroke()

let badgeRect = NSRect(x: 206, y: 315, width: 612, height: 330)
let badge = NSBezierPath(roundedRect: badgeRect, xRadius: 96, yRadius: 96)
NSColor(calibratedWhite: 0.03, alpha: 0.70).setFill()
badge.fill()
NSColor(calibratedWhite: 1, alpha: 0.28).setStroke()
badge.lineWidth = 7
badge.stroke()

let paragraph = NSMutableParagraphStyle()
paragraph.alignment = .center
let attributes: [NSAttributedString.Key: Any] = [
    .font: NSFont.monospacedSystemFont(ofSize: 190, weight: .bold),
    .foregroundColor: NSColor.white,
    .paragraphStyle: paragraph,
    .kern: -9
]
let textRect = NSRect(x: 170, y: 365, width: 684, height: 245)
(version as NSString).draw(in: textRect, withAttributes: attributes)

NSGraphicsContext.restoreGraphicsState()

guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: output))
