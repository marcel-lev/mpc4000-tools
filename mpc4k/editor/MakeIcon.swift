// Renders the app icon (MPC pad grid + USB arrow) as a 1024x1024 PNG.
// Usage: swift MakeIcon.swift <output.png>
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon-1024.png"

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size),
                           pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

let gapMarker1: CGFloat = 300
let gapMarker2: CGFloat = 724
let margin: CGFloat = 100
let bg = NSBezierPath(roundedRect: NSRect(x: margin, y: margin,
                                          width: size - 2 * margin,
                                          height: size - 2 * margin),
                      xRadius: 185, yRadius: 185)
let bgGradient = NSGradient(starting: NSColor(calibratedRed: 0.13, green: 0.15, blue: 0.20, alpha: 1),
                            ending: NSColor(calibratedRed: 0.07, green: 0.08, blue: 0.12, alpha: 1))!
bgGradient.draw(in: bg, angle: -90)

// small 4x4 pad grid, upper area
let grid: CGFloat = 4
let inset: CGFloat = 250
let gap: CGFloat = 26
let top: CGFloat = 160
let cell = (size - 2 * inset - (grid - 1) * gap) / grid
for row in 0..<4 {
    for col in 0..<4 {
        let x = inset + CGFloat(col) * (cell + gap)
        let y = size - top - cell - CGFloat(row) * (cell + gap)
        let pad = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: cell, height: cell),
                               xRadius: 16, yRadius: 16)
        let hue = 0.58 + 0.02 * CGFloat(row)
        NSColor(calibratedHue: hue, saturation: 0.35, brightness: 0.42 + 0.05 * CGFloat(col % 2), alpha: 1).setFill()
        pad.fill()
    }
}

// waveform motif, lower area
let wave = NSBezierPath()
wave.lineWidth = 26
wave.lineCapStyle = .round
let baseY: CGFloat = 300
let heights: [CGFloat] = [30, 78, 130, 96, 150, 60, 112, 140, 82, 48, 100, 36]
var x: CGFloat = 260
for h in heights {
    wave.move(to: NSPoint(x: x, y: baseY - h))
    wave.line(to: NSPoint(x: x, y: baseY + h))
    x += 46
}
NSColor(calibratedRed: 0.36, green: 0.62, blue: 0.94, alpha: 1).setStroke()
wave.stroke()
// loop markers
for mx in [gapMarker1, gapMarker2] {
    let m = NSBezierPath()
    m.lineWidth = 14
    m.move(to: NSPoint(x: mx, y: baseY - 170))
    m.line(to: NSPoint(x: mx, y: baseY + 170))
    NSColor(calibratedRed: 0.95, green: 0.72, blue: 0.25, alpha: 1).setStroke()
    m.stroke()
}

NSGraphicsContext.restoreGraphicsState()
let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
