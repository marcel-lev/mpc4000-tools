// Renders the app icon (MPC-style pad grid) as a 1024x1024 PNG.
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

// macOS icon canvas: rounded square with ~10% margin
let margin: CGFloat = 100
let bg = NSBezierPath(roundedRect: NSRect(x: margin, y: margin,
                                          width: size - 2 * margin,
                                          height: size - 2 * margin),
                      xRadius: 185, yRadius: 185)
let bgGradient = NSGradient(starting: NSColor(calibratedRed: 0.13, green: 0.15, blue: 0.20, alpha: 1),
                            ending: NSColor(calibratedRed: 0.07, green: 0.08, blue: 0.12, alpha: 1))!
bgGradient.draw(in: bg, angle: -90)

// 4x4 MPC pad grid
let grid: CGFloat = 4
let inset: CGFloat = 190
let gap: CGFloat = 34
let cell = (size - 2 * inset - (grid - 1) * gap) / grid
for row in 0..<4 {
    for col in 0..<4 {
        let x = inset + CGFloat(col) * (cell + gap)
        let y = inset + CGFloat(row) * (cell + gap)
        let pad = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: cell, height: cell),
                               xRadius: 26, yRadius: 26)
        let accent = (row == 2 && col == 1)  // one lit pad
        let top: NSColor = accent
            ? NSColor(calibratedRed: 0.98, green: 0.72, blue: 0.25, alpha: 1)
            : NSColor(calibratedHue: 0.60, saturation: 0.35, brightness: 0.50, alpha: 1)
        let bottom: NSColor = accent
            ? NSColor(calibratedRed: 0.88, green: 0.52, blue: 0.10, alpha: 1)
            : NSColor(calibratedHue: 0.62, saturation: 0.38, brightness: 0.36, alpha: 1)
        NSGradient(starting: top, ending: bottom)!.draw(in: pad, angle: -90)
        if accent {
            let glow = NSBezierPath(roundedRect: NSRect(x: x - 10, y: y - 10,
                                                        width: cell + 20, height: cell + 20),
                                    xRadius: 32, yRadius: 32)
            NSColor(calibratedRed: 0.98, green: 0.72, blue: 0.25, alpha: 0.35).setStroke()
            glow.lineWidth = 14
            glow.stroke()
        }
    }
}

NSGraphicsContext.restoreGraphicsState()
let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
