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

// folder glyph, lower area
let folderRect = NSRect(x: 300, y: 190, width: 424, height: 240)
let folder = NSBezierPath(roundedRect: folderRect, xRadius: 28, yRadius: 28)
let tab = NSBezierPath(roundedRect: NSRect(x: 300, y: 400, width: 180, height: 64),
                       xRadius: 20, yRadius: 20)
NSColor(calibratedRed: 0.36, green: 0.62, blue: 0.94, alpha: 1).setFill()
tab.fill()
folder.fill()

// up-down transfer arrows on the folder
let arrow = NSBezierPath()
func arrowPath(cx: CGFloat, cy: CGFloat, up: Bool) {
    let h: CGFloat = 120, w: CGFloat = 84, shaft: CGFloat = 34
    let dir: CGFloat = up ? 1 : -1
    arrow.move(to: NSPoint(x: cx, y: cy + dir * h / 2))
    arrow.line(to: NSPoint(x: cx + w / 2, y: cy + dir * (h / 2 - 52)))
    arrow.line(to: NSPoint(x: cx + shaft / 2, y: cy + dir * (h / 2 - 52)))
    arrow.line(to: NSPoint(x: cx + shaft / 2, y: cy - dir * h / 2))
    arrow.line(to: NSPoint(x: cx - shaft / 2, y: cy - dir * h / 2))
    arrow.line(to: NSPoint(x: cx - shaft / 2, y: cy + dir * (h / 2 - 52)))
    arrow.line(to: NSPoint(x: cx - w / 2, y: cy + dir * (h / 2 - 52)))
    arrow.close()
}
arrowPath(cx: 448, cy: 310, up: true)
arrowPath(cx: 576, cy: 310, up: false)
NSColor(calibratedRed: 0.06, green: 0.10, blue: 0.18, alpha: 0.9).setFill()
arrow.fill()

NSGraphicsContext.restoreGraphicsState()
let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
