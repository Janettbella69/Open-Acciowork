import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ScreenCaptureKit

struct NativeCapture {
    let runID: String
    let token: String
    let windowID: CGWindowID
    let processID: pid_t
    let bundleID: String
    let imageWidth: Int
    let imageHeight: Int
    let bounds: CGRect
    let capturedAt: Date
}

private enum AccessibilityActionError: Error {
    case unavailable
}

@MainActor
enum DesktopAccessibility {
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

    private static func pressTarget(
        from hit: AXUIElement, in window: AXUIElement, expectedLabel: String
    ) -> (AXUIElement, String, String)? {
        var current: AXUIElement? = hit
        let allowedRoles = [
            kAXButtonRole, kAXCheckBoxRole, kAXRadioButtonRole,
            kAXPopUpButtonRole, kAXMenuItemRole,
        ]
        for _ in 0..<5 {
            guard let element = current,
                  let elementWindow = attribute(element, kAXWindowAttribute),
                  CFGetTypeID(elementWindow) == AXUIElementGetTypeID(),
                  CFEqual(elementWindow, window) else { break }
            let role = stringAttribute(element, kAXRoleAttribute)
            let label = [
                stringAttribute(element, kAXTitleAttribute),
                stringAttribute(element, kAXDescriptionAttribute),
            ].compactMap({ $0 }).first(where: { !$0.isEmpty })
            var actions: CFArray?
            if let role, let label,
               allowedRoles.contains(role),
               label == expectedLabel.trimmingCharacters(in: .whitespacesAndNewlines),
               AXUIElementCopyActionNames(element, &actions) == .success,
               let names = actions as? [String],
               names.contains(kAXPressAction) {
                return (element, role, label)
            }
            guard let parent = attribute(element, kAXParentAttribute),
                  CFGetTypeID(parent) == AXUIElementGetTypeID() else { break }
            current = (parent as! AXUIElement)
        }
        return nil
    }

    static func press(
        capture: NativeCapture, x: Int, y: Int, expectedLabel: String
    ) async throws -> [String: Any] {
        guard AXIsProcessTrusted(), capture.processID > 0,
              Date().timeIntervalSince(capture.capturedAt) <= 120 else {
            throw AccessibilityActionError.unavailable
        }
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true
        )
        guard let current = content.windows.first(where: {
            $0.windowID == capture.windowID
                && $0.owningApplication?.processID == capture.processID
                && $0.owningApplication?.bundleIdentifier == capture.bundleID
        }),
              let title = current.title, !title.isEmpty,
              let bounds = windowBounds(
                windowID: capture.windowID, processID: capture.processID
              ),
              matches(bounds, capture.bounds, tolerance: 2) else {
            throw AccessibilityActionError.unavailable
        }
        let point = CGPoint(
            x: bounds.minX + (CGFloat(x) + 0.5) / CGFloat(capture.imageWidth) * bounds.width,
            y: bounds.minY + (CGFloat(y) + 0.5) / CGFloat(capture.imageHeight) * bounds.height
        )
        guard bounds.insetBy(dx: 2, dy: 2).contains(point) else {
            throw AccessibilityActionError.unavailable
        }

        let application = AXUIElementCreateApplication(capture.processID)
        AXUIElementSetMessagingTimeout(application, 2)
        guard let windows = attribute(application, kAXWindowsAttribute) as? [AXUIElement],
              windows.count <= 64 else {
            throw AccessibilityActionError.unavailable
        }
        let candidates = windows.filter { window in
            stringAttribute(window, kAXTitleAttribute) == title
                && rect(window).map { matches($0, bounds, tolerance: 8) } == true
        }
        guard candidates.count == 1, let targetWindow = candidates.first else {
            throw AccessibilityActionError.unavailable
        }
        var hit: AXUIElement?
        guard AXUIElementCopyElementAtPosition(
            application, Float(point.x), Float(point.y), &hit
        ) == .success,
              let hit,
              let (target, role, label) = pressTarget(
                from: hit, in: targetWindow, expectedLabel: expectedLabel
              ) else {
            throw AccessibilityActionError.unavailable
        }

        guard let latestBounds = windowBounds(
            windowID: capture.windowID, processID: capture.processID
        ), matches(latestBounds, bounds, tolerance: 2),
              rect(target)?.contains(point) == true,
              stringAttribute(target, kAXRoleAttribute) == role,
              ([
                  stringAttribute(target, kAXTitleAttribute),
                  stringAttribute(target, kAXDescriptionAttribute),
              ].compactMap({ $0 }).first(where: { !$0.isEmpty })) == label else {
            throw AccessibilityActionError.unavailable
        }

        AXUIElementSetMessagingTimeout(target, 2)
        let result = AXUIElementPerformAction(target, kAXPressAction as CFString)
        return [
            "window_id": Int(capture.windowID),
            "process_id": Int(capture.processID),
            "bundle_id": capture.bundleID,
            "title": title,
            "control_label": label,
            "control_role": role,
            "outcome": result == .success ? "confirmed" : "uncertain",
            "ax_error_code": Int(result.rawValue),
        ]
    }
}
