import Foundation
import Vision
import AppKit

let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("Failed to load image\n", stderr)
    exit(2)
}
let request = VNRecognizeTextRequest { request, error in
    if let error { fputs("\(error)\n", stderr); exit(3) }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try handler.perform([request])
let observations = (request.results ?? []) as [VNRecognizedTextObservation]
var out: [String] = []
for obs in observations {
    guard let top = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox
    let x = b.origin.x * Double(cg.width)
    let y = Double(cg.height) - (b.origin.y + b.height) * Double(cg.height)
    let textData = try JSONSerialization.data(withJSONObject: [top.string])
    let textB64 = textData.base64EncodedString()
    out.append(String(format: "{\"text\":\"%@\",\"confidence\":%.3f,\"x\":%.1f,\"y\":%.1f,\"width\":%.1f,\"height\":%.1f}",
                      textB64, top.confidence, x, y, b.width * Double(cg.width), b.height * Double(cg.height)))
}
let json = "{\"imageWidth\":\(cg.width),\"imageHeight\":\(cg.height),\"items\":[\(out.joined(separator: ","))]}"
FileHandle.standardOutput.write(json.data(using: .utf8)!)
