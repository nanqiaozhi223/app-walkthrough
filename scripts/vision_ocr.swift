// vision_ocr.swift — standalone macOS OCR helper built on the Vision framework.
// Usage: vision_ocr <image-path>
// Prints a JSON array of recognised text items to stdout.
// Coordinate convention: pixels with a top-left origin.
import Foundation
import Vision
import ImageIO
import CoreGraphics

struct OCRItem {
    var text: String
    var confidence: Double
    var x: Int
    var y: Int
    var width: Int
    var height: Int
    var centerY: Double
}

func failAndExit(_ message: String) -> Never {
    let escaped = message.replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
        .replacingOccurrences(of: "\n", with: " ")
    print("{\"error\": \"\(escaped)\"}")
    exit(1)
}

func loadCGImage(path: String) -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        failAndExit("cannot open image: \(path)")
    }
    guard let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        failAndExit("cannot decode image: \(path)")
    }
    return image
}

// Vision bounding boxes are normalized with a bottom-left origin.
// Convert to integer pixel rectangles with a top-left origin.
func pixelRect(observation: VNRecognizedTextObservation,
               pixelWidth: Int, pixelHeight: Int) -> (Int, Int, Int, Int) {
    let box = observation.boundingBox
    let w = box.width * CGFloat(pixelWidth)
    let h = box.height * CGFloat(pixelHeight)
    let x = box.origin.x * CGFloat(pixelWidth)
    // Top-left y: flip the bottom-left origin vertically.
    let y = (1.0 - box.origin.y - box.height) * CGFloat(pixelHeight)
    return (Int(x.rounded()), Int(y.rounded()),
            Int(w.rounded()), Int(h.rounded()))
}

func jsonString(_ raw: String) -> String {
    var out = "\""
    for scalar in raw.unicodeScalars {
        switch scalar {
        case "\\": out += "\\\\"
        case "\"": out += "\\\""
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default:
            if scalar.value < 0x20 {
                out += String(format: "\\u%04x", scalar.value)
            } else {
                out.unicodeScalars.append(scalar)
            }
        }
    }
    out += "\""
    return out
}

guard CommandLine.arguments.count == 2 else {
    failAndExit("usage: vision_ocr <image-path>")
}
let imagePath = CommandLine.arguments[1]
let cgImage = loadCGImage(path: imagePath)
let pixelWidth = cgImage.width
let pixelHeight = cgImage.height

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

// Prefer Simplified Chinese plus English; gracefully drop any language
// the current macOS build does not support for text recognition.
let wantedLanguages = ["zh-Hans", "en-US"]
if let supported = try? request.supportedRecognitionLanguages() {
    let usable = wantedLanguages.filter { supported.contains($0) }
    if !usable.isEmpty {
        request.recognitionLanguages = usable
    }
}

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    failAndExit("Vision request failed: \(error.localizedDescription)")
}

guard let observations = request.results else {
    print("[]")
    exit(0)
}

var items: [OCRItem] = []
for observation in observations {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let (x, y, w, h) = pixelRect(observation: observation,
                                 pixelWidth: pixelWidth,
                                 pixelHeight: pixelHeight)
    items.append(OCRItem(
        text: candidate.string,
        confidence: Double(candidate.confidence),
        x: x, y: y, width: w, height: h,
        centerY: Double(y) + Double(h) / 2.0
    ))
}

// Sort top-to-bottom, then left-to-right. Boxes whose vertical centres fall
// within roughly half a box height of each other are treated as one row.
items.sort { lhs, rhs in
    let tolerance = Double(max(lhs.height, rhs.height)) * 0.5
    if abs(lhs.centerY - rhs.centerY) <= max(tolerance, 1.0) {
        return lhs.x < rhs.x
    }
    return lhs.centerY < rhs.centerY
}

var lines: [String] = ["["]
for (index, item) in items.enumerated() {
    let confidence = String(format: "%.4f", item.confidence)
    var line = "  {"
    line += "\"text\": \(jsonString(item.text)), "
    line += "\"confidence\": \(confidence), "
    line += "\"x\": \(item.x), \"y\": \(item.y), "
    line += "\"width\": \(item.width), \"height\": \(item.height)"
    line += index == items.count - 1 ? "}" : "},"
    lines.append(line)
}
lines.append("]")
print(lines.joined(separator: "\n"))
