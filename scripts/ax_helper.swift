// ax_helper.swift — standalone macOS Accessibility helper for desktop app walkthroughs.
//
// Usage:
//   ax_helper dump PID                 -> JSON array of visible AX elements
//   ax_helper press PID TEXT [INDEX]   -> find TEXT and perform kAXPressAction
//   ax_helper windows PID              -> on-screen windows of PID via CGWindowList
//
// All output is UTF-8 JSON on stdout. On failure the helper prints
// {"error": "..."} and exits 1, so the Python driver can surface a Chinese hint.
import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

// MARK: - JSON output

func emitJSON<T: Encodable>(_ payload: T) -> Never {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    do {
        let data = try encoder.encode(payload)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    } catch {
        print("{\"error\": \"JSON encoding failed: \(error.localizedDescription)\"}")
    }
    exit(0)
}

func failAndExit(_ message: String) -> Never {
    struct ErrorEnvelope: Encodable {
        let error: String
    }
    let encoder = JSONEncoder()
    let data = (try? encoder.encode(ErrorEnvelope(error: message))) ?? Data("{\"error\":\"unknown\"}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
    exit(1)
}

// MARK: - AX attribute helpers

func axAttribute(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    return result == .success ? value : nil
}

func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    guard let raw = axAttribute(element, attribute) else { return nil }
    if let text = raw as? String {
        return text.isEmpty ? nil : text
    }
    // Some apps expose NSNumber/attributed values; normalise to text when possible.
    if let number = raw as? NSNumber {
        return number.stringValue
    }
    return nil
}

func axBool(_ element: AXUIElement, _ attribute: String) -> Bool? {
    guard let raw = axAttribute(element, attribute) else { return nil }
    if let flag = raw as? Bool {
        return flag
    }
    if let number = raw as? NSNumber {
        return number.boolValue
    }
    return nil
}

/// AXValue position/size become plain numeric tuples; anything else returns nil.
func axPoint(_ raw: CFTypeRef?) -> CGPoint? {
    guard let value = raw, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    // swiftlint:disable:next force_cast
    let axValue = value as! AXValue
    var point = CGPoint.zero
    return AXValueGetValue(axValue, .cgPoint, &point) ? point : nil
}

func axSize(_ raw: CFTypeRef?) -> CGSize? {
    guard let value = raw, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    // swiftlint:disable:next force_cast
    let axValue = value as! AXValue
    var size = CGSize.zero
    return AXValueGetValue(axValue, .cgSize, &size) ? size : nil
}

func rounded(_ value: CGFloat) -> Double {
    (value * 10).rounded() / 10
}

// MARK: - Element model

struct AXElementRecord: Encodable {
    let role: String?
    let title: String?
    let value: String?
    let description: String?
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let enabled: Bool?
    let focused: Bool?
}

// Walk limits keep one misbehaving app tree from turning into an unbounded dump.
let maxDepth = 12
let maxElements = 1000

func collectElements(application: AXUIElement, root: AXUIElement) -> [AXElementRecord] {
    var records: [AXElementRecord] = []
    // AXUIElement is a CFType; track identities to avoid loops between parents/children.
    var visited = Set<CFHashCode>()
    var total = 0

    func record(for element: AXUIElement) -> AXElementRecord? {
        let point = axPoint(axAttribute(element, kAXPositionAttribute as String))
        let size = axSize(axAttribute(element, kAXSizeAttribute as String))
        guard let origin = point, let bounds = size else { return nil }
        // Keep elements with real, on-screen-ish bounds; zero-size layout nodes add noise.
        guard bounds.width > 0, bounds.height > 0 else { return nil }
        return AXElementRecord(
            role: axString(element, kAXRoleAttribute as String),
            title: axString(element, kAXTitleAttribute as String),
            value: axString(element, kAXValueAttribute as String),
            description: axString(element, kAXDescriptionAttribute as String),
            x: rounded(origin.x),
            y: rounded(origin.y),
            width: rounded(bounds.width),
            height: rounded(bounds.height),
            enabled: axBool(element, kAXEnabledAttribute as String),
            focused: axBool(element, kAXFocusedAttribute as String)
        )
    }

    func walk(_ element: AXUIElement, depth: Int) {
        if total >= maxElements || depth > maxDepth { return }
        let identity = CFHash(element)
        if visited.contains(identity) { return }
        visited.insert(identity)
        total += 1
        if let item = record(for: element) {
            records.append(item)
        }
        guard depth < maxDepth else { return }
        let children = (axAttribute(element, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
        for child in children {
            walk(child, depth: depth + 1)
            if total >= maxElements { break }
        }
    }

    walk(root, depth: 0)
    // The application node itself often lacks a position; include it defensively if it has one.
    if total < maxElements, CFHash(application) != CFHash(root), let item = record(for: application) {
        records.insert(item, at: 0)
    }
    return records
}

func pickWindow(application: AXUIElement) -> AXUIElement? {
    if let focused = axAttribute(application, kAXFocusedWindowAttribute as String) {
        // swiftlint:disable:next force_cast
        return (focused as! AXUIElement)
    }
    let windows = (axAttribute(application, kAXWindowsAttribute as String) as? [AXUIElement]) ?? []
    return windows.first
}

// MARK: - dump

func requireAccessibility() {
    // Without this grant every AX copy silently fails and the dump comes back empty;
    // fail loudly so the Python driver can point the user at System Settings.
    if !AXIsProcessTrusted() {
        failAndExit("缺少辅助功能（Accessibility）权限：请到 系统设置 > 隐私与安全性 > 辅助功能 中允许运行本工具的终端程序")
    }
}

func runDump(pid: pid_t) -> Never {
    requireAccessibility()
    let application = AXUIElementCreateApplication(pid)
    // A benign copy probes the trusted-process state and fails with a clear error.
    guard let window = pickWindow(application: application) else {
        // Some apps expose no windows but still have a root element; fall back to the app node.
        emitJSON(collectElements(application: application, root: application))
    }
    emitJSON(collectElements(application: application, root: window))
}

// MARK: - press

struct PressCandidate: Encodable {
    let index: Int
    let role: String?
    let title: String?
    let value: String?
    let description: String?
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let enabled: Bool?
    let focused: Bool?
    let match: String
}

struct PressResult: Encodable {
    let ok: Bool
    let matched: Int
    let exact: Int
    let contains: Int
    let index: Int?
    let selected: PressCandidate?
    let message: String?
}

func textFields(of element: AXUIElement) -> [String] {
    [
        axString(element, kAXTitleAttribute as String),
        axString(element, kAXValueAttribute as String),
        axString(element, kAXDescriptionAttribute as String),
    ].compactMap { $0 }
}

func candidate(from element: AXUIElement, index: Int, match: String) -> PressCandidate? {
    let point = axPoint(axAttribute(element, kAXPositionAttribute as String))
    let size = axSize(axAttribute(element, kAXSizeAttribute as String))
    guard let origin = point, let bounds = size, bounds.width > 0, bounds.height > 0 else { return nil }
    return PressCandidate(
        index: index,
        role: axString(element, kAXRoleAttribute as String),
        title: axString(element, kAXTitleAttribute as String),
        value: axString(element, kAXValueAttribute as String),
        description: axString(element, kAXDescriptionAttribute as String),
        x: rounded(origin.x),
        y: rounded(origin.y),
        width: rounded(bounds.width),
        height: rounded(bounds.height),
        enabled: axBool(element, kAXEnabledAttribute as String),
        focused: axBool(element, kAXFocusedAttribute as String),
        match: match
    )
}

func runPress(pid: pid_t, needle: String, requestedIndex: Int?) -> Never {
    requireAccessibility()
    let application = AXUIElementCreateApplication(pid)
    guard let window = pickWindow(application: application) else {
        emitJSON(PressResult(ok: false, matched: 0, exact: 0, contains: 0, index: nil, selected: nil,
                             message: "应用没有可访问的窗口"))
    }

    var exactElements: [AXUIElement] = []
    var containsElements: [AXUIElement] = []
    var visited = Set<CFHashCode>()
    var total = 0

    func walk(_ element: AXUIElement, depth: Int) {
        if total >= maxElements || depth > maxDepth { return }
        let identity = CFHash(element)
        if visited.contains(identity) { return }
        visited.insert(identity)
        total += 1
        let fields = textFields(of: element)
        if fields.contains(needle) {
            exactElements.append(element)
        } else if fields.contains(where: { $0.localizedCaseInsensitiveContains(needle) }) {
            containsElements.append(element)
        }
        guard depth < maxDepth else { return }
        let children = (axAttribute(element, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
        for child in children {
            walk(child, depth: depth + 1)
            if total >= maxElements { break }
        }
    }
    walk(window, depth: 0)

    // Exact matches always win; contains matches are only used when nothing matches exactly.
    let useExact = !exactElements.isEmpty
    let chosenElements = useExact ? exactElements : containsElements
    // Build candidates in lockstep so the caller's INDEX always addresses the
    // same element; elements without bounds are skipped in both lists together.
    var candidates: [PressCandidate] = []
    var candidateElements: [AXUIElement] = []
    for element in chosenElements {
        if let item = candidate(from: element, index: candidates.count, match: useExact ? "exact" : "contains") {
            candidates.append(item)
            candidateElements.append(element)
        }
    }

    guard !candidates.isEmpty else {
        emitJSON(PressResult(ok: false, matched: 0, exact: exactElements.count, contains: containsElements.count,
                             index: nil, selected: nil, message: "没有找到与“\(needle)”匹配的可访问元素，拒绝点击"))
    }

    let index = requestedIndex ?? 0
    if index < 0 || index >= candidates.count {
        emitJSON(PressResult(ok: false, matched: candidates.count, exact: exactElements.count,
                             contains: containsElements.count, index: index, selected: nil,
                             message: "INDEX 超出范围：0..\(candidates.count - 1)"))
    }
    let target = candidateElements[index]

    let actions = (axAttribute(target, "AXActions") as? [String]) ?? []
    guard actions.contains(kAXPressAction as String) else {
        emitJSON(PressResult(ok: false, matched: candidates.count, exact: exactElements.count,
                             contains: containsElements.count, index: index, selected: candidates[index],
                             message: "目标元素不支持 kAXPressAction（role=\(candidates[index].role ?? "?")），拒绝操作"))
    }
    let pressResult = AXUIElementPerformAction(target, kAXPressAction as CFString)
    guard pressResult == .success else {
        emitJSON(PressResult(ok: false, matched: candidates.count, exact: exactElements.count,
                             contains: containsElements.count, index: index, selected: candidates[index],
                             message: "kAXPressAction 执行失败（AXError \(pressResult.rawValue)）"))
    }
    emitJSON(PressResult(ok: true, matched: candidates.count, exact: exactElements.count,
                         contains: containsElements.count, index: index, selected: candidates[index], message: nil))
}

// MARK: - windows

struct WindowRecord: Encodable {
    let window_id: Int
    let title: String?
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

func runWindows(pid: pid_t) -> Never {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let info = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        emitJSON([WindowRecord]())
    }
    var windows: [WindowRecord] = []
    for entry in info {
        guard let ownerPID = entry[kCGWindowOwnerPID as String] as? Int, ownerPID == Int(pid) else { continue }
        // Skip menu-bar/menus and other auxiliary layers; keep ordinary on-screen windows.
        let layer = entry[kCGWindowLayer as String] as? Int ?? 0
        guard layer == 0 else { continue }
        let bounds = entry[kCGWindowBounds as String] as? [String: CGFloat] ?? [:]
        let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary) ?? .zero
        guard rect.width > 0, rect.height > 0 else { continue }
        let windowID = entry[kCGWindowNumber as String] as? Int ?? 0
        let title = entry[kCGWindowName as String] as? String
        windows.append(WindowRecord(
            window_id: windowID,
            title: title?.isEmpty == false ? title : nil,
            x: rounded(rect.origin.x),
            y: rounded(rect.origin.y),
            width: rounded(rect.width),
            height: rounded(rect.height)
        ))
    }
    emitJSON(windows)
}

// MARK: - entry point

let arguments = CommandLine.arguments
guard arguments.count >= 3, let pid = pid_t(arguments[2]) else {
    failAndExit("usage: ax_helper dump|press|windows PID [TEXT] [INDEX]")
}

switch arguments[1] {
case "dump":
    runDump(pid: pid)
case "press":
    guard arguments.count >= 4 else { failAndExit("usage: ax_helper press PID TEXT [INDEX]") }
    let index: Int? = arguments.count >= 5 ? Int(arguments[4]) : nil
    if arguments.count >= 5, index == nil {
        failAndExit("INDEX 必须是整数：\(arguments[4])")
    }
    runPress(pid: pid, needle: arguments[3], requestedIndex: index)
case "windows":
    runWindows(pid: pid)
default:
    failAndExit("未知命令：\(arguments[1])（支持 dump / press / windows）")
}
