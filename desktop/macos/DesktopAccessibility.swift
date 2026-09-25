import AppKit
import ApplicationServices
import Carbon
import CoreGraphics
import Foundation

struct NativeCapture {
    let runID: String
    let token: String
    let windowID: CGWindowID
    let processID: pid_t
    let bundleID: String
    let title: String
    let imageWidth: Int
    let imageHeight: Int
    let bounds: CGRect
    let capturedAt: Date
}

private enum AccessibilityActionError: Error {
    case unavailable
}

private struct WindowContext {
    let application: AXUIElement
    let window: AXUIElement
    let title: String
    let bounds: CGRect
}

@MainActor
enum DesktopAccessibility {
    static let supportedKeys: Set<String> = [
        "Return", "Escape", "Tab", "Space", "Backspace", "Up", "Down", "Left", "Right",
    ]

    static func windowBounds(windowID: CGWindowID, processID: pid_t) -> CGRect? {
        guard let windows = CGWindowListCopyWindowInfo(
            .optionOnScreenOnly, kCGNullWindowID
        ) as? [[String: Any]] else { return nil }
        for item in windows {
            guard let number = item[kCGWindowNumber as String] as? NSNumber,
                  number.uint32Value == windowID,
                  let owner = item[kCGWindowOwnerPID as String] as? NSNumber,
                  owner.int32Value == processID,
                  let layer = item[kCGWindowLayer as String] as? NSNumber,
                  layer.intValue == 0,
                  let rawBounds = item[kCGWindowBounds as String],
                  CFGetTypeID(rawBounds as CFTypeRef) == CFDictionaryGetTypeID() else {
                continue
            }
            let dictionary = rawBounds as! CFDictionary
            var bounds = CGRect.zero
            if CGRectMakeWithDictionaryRepresentation(dictionary, &bounds),
               bounds.width > 0, bounds.height > 0 {
                return bounds
            }
        }
        return nil
    }

    private static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
            return nil
        }
        return value
    }

    private static func stringAttribute(_ element: AXUIElement, _ name: String) -> String? {
        attribute(element, name) as? String
    }

    private static func rect(_ element: AXUIElement) -> CGRect? {
        guard let rawPosition = attribute(element, kAXPositionAttribute),
              let rawSize = attribute(element, kAXSizeAttribute),
              CFGetTypeID(rawPosition) == AXValueGetTypeID(),
              CFGetTypeID(rawSize) == AXValueGetTypeID() else {
            return nil
        }
        let position = rawPosition as! AXValue
        let size = rawSize as! AXValue
        var point = CGPoint.zero
        var dimensions = CGSize.zero
        guard AXValueGetValue(position, .cgPoint, &point),
              AXValueGetValue(size, .cgSize, &dimensions) else {
            return nil
        }
        return CGRect(origin: point, size: dimensions)
    }

    private static func matches(_ left: CGRect, _ right: CGRect, tolerance: CGFloat) -> Bool {
        abs(left.origin.x - right.origin.x) <= tolerance
            && abs(left.origin.y - right.origin.y) <= tolerance
            && abs(left.width - right.width) <= tolerance
            && abs(left.height - right.height) <= tolerance
    }

    private static func validatedWindow(capture: NativeCapture) throws -> WindowContext {
        guard AXIsProcessTrusted(), capture.processID > 0,
              Date().timeIntervalSince(capture.capturedAt) <= 120,
              NSRunningApplication(processIdentifier: capture.processID)?.bundleIdentifier
                == capture.bundleID,
              !capture.title.isEmpty,
              let bounds = windowBounds(
                windowID: capture.windowID, processID: capture.processID
              ),
              matches(bounds, capture.bounds, tolerance: 2) else {
            throw AccessibilityActionError.unavailable
        }
        let application = AXUIElementCreateApplication(capture.processID)
        AXUIElementSetMessagingTimeout(application, 1)
        guard let windows = attribute(application, kAXWindowsAttribute) as? [AXUIElement],
              windows.count <= 64 else {
            throw AccessibilityActionError.unavailable
        }
        let candidates = windows.filter { window in
            stringAttribute(window, kAXTitleAttribute) == capture.title
                && rect(window).map { matches($0, bounds, tolerance: 8) } == true
        }
        guard candidates.count == 1, let targetWindow = candidates.first else {
            throw AccessibilityActionError.unavailable
        }
        return WindowContext(
            application: application, window: targetWindow, title: capture.title, bounds: bounds
        )
    }

    private static func imagePoint(
        capture: NativeCapture, bounds: CGRect, x: Int, y: Int
    ) throws -> CGPoint {
        let point = CGPoint(
            x: bounds.minX + (CGFloat(x) + 0.5) / CGFloat(capture.imageWidth) * bounds.width,
            y: bounds.minY + (CGFloat(y) + 0.5) / CGFloat(capture.imageHeight) * bounds.height
        )
        guard bounds.insetBy(dx: 2, dy: 2).contains(point) else {
            throw AccessibilityActionError.unavailable
        }
        return point
    }

    private static func labelledTarget(
        application: AXUIElement, window: AXUIElement,
        point: CGPoint, expectedLabel: String, allowedRoles: Set<String>
    ) throws -> (AXUIElement, String, String) {
        var hit: AXUIElement?
        guard AXUIElementCopyElementAtPosition(
            application, Float(point.x), Float(point.y), &hit
        ) == .success, let hit else {
            throw AccessibilityActionError.unavailable
        }
        var current: AXUIElement? = hit
        for _ in 0..<5 {
            guard let element = current else { break }
            let elementWindow = attribute(element, kAXWindowAttribute)
            if let elementWindow,
               (CFGetTypeID(elementWindow) != AXUIElementGetTypeID()
                    || !CFEqual(elementWindow, window)) {
                break
            }
            let role = stringAttribute(element, kAXRoleAttribute)
            let label = [
                stringAttribute(element, kAXTitleAttribute),
                stringAttribute(element, kAXDescriptionAttribute),
            ].compactMap({ $0 }).first(where: { !$0.isEmpty })
            if let role, let label,
               elementWindow != nil,
               allowedRoles.contains(role), label == expectedLabel {
                return (element, role, label)
            }
            guard let parent = attribute(element, kAXParentAttribute),
                  CFGetTypeID(parent) == AXUIElementGetTypeID() else { break }
            current = (parent as! AXUIElement)
        }
        throw AccessibilityActionError.unavailable
    }

    private static func recheckTarget(
        capture: NativeCapture, context: WindowContext,
        target: AXUIElement, point: CGPoint, role: String, label: String
    ) -> Bool {
        guard let latestBounds = windowBounds(
            windowID: capture.windowID, processID: capture.processID
        ), matches(latestBounds, context.bounds, tolerance: 2),
              rect(target)?.contains(point) == true,
              stringAttribute(target, kAXRoleAttribute) == role,
              let targetWindow = attribute(target, kAXWindowAttribute),
              CFGetTypeID(targetWindow) == AXUIElementGetTypeID(),
              CFEqual(targetWindow, context.window),
              ([
                  stringAttribute(target, kAXTitleAttribute),
                  stringAttribute(target, kAXDescriptionAttribute),
              ].compactMap({ $0 }).first(where: { !$0.isEmpty })) == label else { return false }
        return true
    }

    static func press(
        capture: NativeCapture, x: Int, y: Int, expectedLabel: String
    ) throws -> [String: Any] {
        let context = try validatedWindow(capture: capture)
        let point = try imagePoint(capture: capture, bounds: context.bounds, x: x, y: y)
        let (target, role, label) = try labelledTarget(
            application: context.application, window: context.window,
            point: point, expectedLabel: expectedLabel,
            allowedRoles: [
                kAXButtonRole, kAXCheckBoxRole, kAXRadioButtonRole,
                kAXPopUpButtonRole, kAXMenuItemRole,
            ]
        )
        var actions: CFArray?
        guard recheckTarget(
            capture: capture, context: context, target: target,
            point: point, role: role, label: label
        ), AXUIElementCopyActionNames(target, &actions) == .success,
           let names = actions as? [String], names.contains(kAXPressAction) else {
            throw AccessibilityActionError.unavailable
        }

        AXUIElementSetMessagingTimeout(target, 2)
        let result = AXUIElementPerformAction(target, kAXPressAction as CFString)
        return [
            "window_id": Int(capture.windowID),
            "process_id": Int(capture.processID),
            "bundle_id": capture.bundleID,
            "title": context.title,
            "control_label": label,
            "control_role": role,
            "outcome": result == .success ? "confirmed" : "uncertain",
            "ax_error_code": Int(result.rawValue),
        ]
    }

    static func replaceText(
        capture: NativeCapture, x: Int, y: Int, expectedLabel: String,
        expectedCurrent: String, replacement: String
    ) throws -> [String: Any] {
        let context = try validatedWindow(capture: capture)
        let point = try imagePoint(capture: capture, bounds: context.bounds, x: x, y: y)
        let (target, role, label) = try labelledTarget(
            application: context.application, window: context.window,
            point: point, expectedLabel: expectedLabel,
            allowedRoles: [kAXTextFieldRole, kAXTextAreaRole]
        )
        var settable = DarwinBoolean(false)
        guard stringAttribute(target, kAXSubroleAttribute) != kAXSecureTextFieldSubrole,
              recheckTarget(
                capture: capture, context: context, target: target,
                point: point, role: role, label: label
              ),
              AXUIElementIsAttributeSettable(
                target, kAXValueAttribute as CFString, &settable
              ) == .success, settable.boolValue,
              stringAttribute(target, kAXValueAttribute) == expectedCurrent else {
            throw AccessibilityActionError.unavailable
        }
        AXUIElementSetMessagingTimeout(target, 2)
        let result = AXUIElementSetAttributeValue(
            target, kAXValueAttribute as CFString, replacement as CFString
        )
        let observed = stringAttribute(target, kAXValueAttribute)
        return [
            "window_id": Int(capture.windowID),
            "process_id": Int(capture.processID),
            "bundle_id": capture.bundleID,
            "title": context.title,
            "control_label": label,
            "control_role": role,
            "outcome": result == .success && observed == replacement ? "confirmed" : "uncertain",
            "ax_error_code": Int(result.rawValue),
        ]
    }

    static func sendKey(capture: NativeCapture, key: String) throws -> [String: Any] {
        let context = try validatedWindow(capture: capture)
        let modifierMask: CGEventFlags = [
            .maskShift, .maskControl, .maskAlternate, .maskCommand,
            .maskSecondaryFn, .maskAlphaShift, .maskHelp,
        ]
        guard let focused = attribute(context.application, kAXFocusedWindowAttribute),
              CFGetTypeID(focused) == AXUIElementGetTypeID(),
              CFEqual(focused, context.window),
              CGEventSource.flagsState(.combinedSessionState)
                .intersection(modifierMask).isEmpty,
              let code = keyCode(key),
              let source = CGEventSource(stateID: .privateState),
              let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false),
              let latestBounds = windowBounds(
                windowID: capture.windowID, processID: capture.processID
              ), matches(latestBounds, context.bounds, tolerance: 2),
              CGEventSource.flagsState(.combinedSessionState)
                .intersection(modifierMask).isEmpty else {
            throw AccessibilityActionError.unavailable
        }
        down.flags = []
        up.flags = []
        // PID targeting confines the event to the approved app. A post has no
        // delivery acknowledgement, so the result is "dispatched", not verified.
        down.postToPid(capture.processID)
        up.postToPid(capture.processID)
        return [
            "window_id": Int(capture.windowID),
            "process_id": Int(capture.processID),
            "bundle_id": capture.bundleID,
            "title": context.title,
            "key": key,
            "outcome": "dispatched",
        ]
    }

    private static func keyCode(_ key: String) -> CGKeyCode? {
        switch key {
        case "Return": CGKeyCode(kVK_Return)
        case "Escape": CGKeyCode(kVK_Escape)
        case "Tab": CGKeyCode(kVK_Tab)
        case "Space": CGKeyCode(kVK_Space)
        case "Backspace": CGKeyCode(kVK_Delete)
        case "Up": CGKeyCode(kVK_UpArrow)
        case "Down": CGKeyCode(kVK_DownArrow)
        case "Left": CGKeyCode(kVK_LeftArrow)
        case "Right": CGKeyCode(kVK_RightArrow)
        default: nil
        }
    }
}
