import AppKit
import ApplicationServices
import Darwin
import Foundation
import ScreenCaptureKit
import Security
import WebKit

private enum DesktopError: LocalizedError {
    case missingResource(String)
    case invalidHandshake
    case startupFailed

    var errorDescription: String? {
        switch self {
        case .missingResource(let name): "Missing desktop resource: \(name)"
        case .invalidHandshake: "The local service returned an invalid startup response."
        case .startupFailed: "The local service did not become ready."
        }
    }
}

private struct SidecarHandshake: Decodable {
    let port: Int
}

private struct Sidecar {
    let process: Process
    let input: Pipe
    let output: Pipe
    let port: Int
    let token: String
}

private struct NativeRequest: Decodable {
    let v: Int
    let id: String
    let op: String
    let run_id: String?
    let args: NativeArguments?
}

private struct NativeArguments: Decodable {
    let capture_token: String?
    let x: Int?
    let y: Int?
    let width: Int?
    let height: Int?
    let expected_label: String?
    let expected_current_text: String?
    let replacement_text: String?
    let expected_window_title: String?
    let key: String?
}

private final class ReadyState: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    func set(_ value: Bool) {
        lock.lock()
        self.value = value
        lock.unlock()
    }

    func get() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

@MainActor
private final class DesktopApplication: NSObject, NSApplicationDelegate,
    WKNavigationDelegate, @preconcurrency SCContentSharingPickerObserver {
    private var sidecar: Sidecar?
    private var window: NSWindow?
    private var webView: WKWebView?
    private var initialPageLoaded = false
    private var pickerRegistered = false
    private var pendingPickerRequestID: String?
    private var pendingPickerRunID: String?
    private var selectedWindow: SCWindow?
    private var selectedFilter: SCContentFilter?
    private var selectedRunID: String?
    private var lastCapture: NativeCapture?
    private var activeCaptureRequestID: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let sidecar = try Self.startSidecar()
                DispatchQueue.main.async { self.openWindow(for: sidecar) }
            } catch {
                DispatchQueue.main.async { self.showStartupError(error) }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        sidecar?.process.terminate()
    }

    private nonisolated static func randomToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        let result = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        precondition(result == errSecSuccess, "System random generator is unavailable")
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    private nonisolated static func readLine(_ handle: FileHandle) throws -> Data {
        var line = Data()
        let deadline = DispatchTime.now().uptimeNanoseconds + 15_000_000_000
        while line.count < 256 {
            let now = DispatchTime.now().uptimeNanoseconds
            guard now < deadline else { throw DesktopError.startupFailed }
            var descriptor = pollfd(fd: handle.fileDescriptor, events: Int16(POLLIN), revents: 0)
            let remainingMilliseconds = Int32(min((deadline - now) / 1_000_000, 15_000))
            guard poll(&descriptor, 1, remainingMilliseconds) > 0 else {
                throw DesktopError.startupFailed
            }
            guard let byte = try handle.read(upToCount: 1), !byte.isEmpty else {
                throw DesktopError.invalidHandshake
            }
            if byte == Data([10]) { return line }
            line.append(byte)
        }
        throw DesktopError.invalidHandshake
    }

    private nonisolated static func readNativeLine(_ handle: FileHandle) throws -> Data? {
        var line = Data()
        while line.count < 16_384 {
            guard let byte = try handle.read(upToCount: 1), !byte.isEmpty else {
                return nil
            }
            if byte == Data([10]) { return line }
            line.append(byte)
        }
        throw DesktopError.invalidHandshake
    }

    private nonisolated static func ready(port: Int, token: String) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        var request = URLRequest(url: url)
        request.setValue(token, forHTTPHeaderField: "X-Ophira-Desktop-Token")
        let semaphore = DispatchSemaphore(value: 0)
        let state = ReadyState()
        let task = URLSession.shared.dataTask(with: request) { _, response, _ in
            state.set((response as? HTTPURLResponse)?.statusCode == 200)
            semaphore.signal()
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + 2) == .success else {
            task.cancel()
            return false
        }
        return state.get()
    }

    private nonisolated static func startSidecar() throws -> Sidecar {
        guard let resources = Bundle.main.resourceURL else {
            throw DesktopError.missingResource("bundle resources")
        }
        let ui = resources.appendingPathComponent("ui")
        let backend = resources.appendingPathComponent("backend/src")
        let plugin = resources.appendingPathComponent("backend/ophira-capabilities")
        let defaultPython = Bundle.main.object(forInfoDictionaryKey: "OphiraPythonExecutable") as? String
        guard let pythonPath = ProcessInfo.processInfo.environment["OPHIRA_DESKTOP_PYTHON"]
            ?? defaultPython else {
            throw DesktopError.missingResource("Python runtime")
        }
        guard FileManager.default.isExecutableFile(atPath: pythonPath) else {
            throw DesktopError.missingResource("Python runtime")
        }
        guard FileManager.default.fileExists(atPath: ui.appendingPathComponent("index.html").path),
              FileManager.default.fileExists(atPath: backend.path),
              FileManager.default.fileExists(atPath: plugin.path) else {
            throw DesktopError.missingResource("UI or backend")
        }

        let defaultSupport = try FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true
        ).appendingPathComponent("OPHIRA", isDirectory: true)
        let support = ProcessInfo.processInfo.environment["OPHIRA_DESKTOP_DATA_DIR"]
            .map { URL(fileURLWithPath: $0, isDirectory: true) } ?? defaultSupport
        try FileManager.default.createDirectory(
            at: support, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: support.path
        )

        let token = randomToken()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [
            "-m", "ophira_backend.desktop_server",
            "--ui-dir", ui.path,
            "--data-dir", support.path,
            "--plugin-dir", plugin.path,
        ]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = backend.path
        process.environment = environment
        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.standardError
        try process.run()
        input.fileHandleForWriting.write(Data((token + "\n").utf8))

        do {
            let line = try readLine(output.fileHandleForReading)
            let handshake = try JSONDecoder().decode(SidecarHandshake.self, from: line)
            guard 1...65_535 ~= handshake.port else { throw DesktopError.invalidHandshake }
            for _ in 0..<40 {
                if !process.isRunning { throw DesktopError.startupFailed }
                if ready(port: handshake.port, token: token) {
                    return Sidecar(
                        process: process, input: input, output: output,
                        port: handshake.port, token: token
                    )
                }
                Thread.sleep(forTimeInterval: 0.25)
            }
            throw DesktopError.startupFailed
        } catch {
            if process.isRunning { process.terminate() }
            throw error
        }
    }

    private func openWindow(for sidecar: Sidecar) {
        self.sidecar = sidecar
        beginNativeCommands(for: sidecar)
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        self.webView = webView

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        window.title = "OPHIRA"
        window.center()
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        self.window = window
        NSApp.activate(ignoringOtherApps: true)

        let properties: [HTTPCookiePropertyKey: Any] = [
            .name: "ophira_desktop_session",
            .value: sidecar.token,
            .domain: "127.0.0.1",
            .path: "/",
            HTTPCookiePropertyKey("HttpOnly"): "TRUE",
            HTTPCookiePropertyKey("SameSite"): "Strict",
        ]
        guard let cookie = HTTPCookie(properties: properties),
              let url = URL(string: "http://127.0.0.1:\(sidecar.port)/app") else {
            showStartupError(DesktopError.invalidHandshake)
            return
        }
        configuration.websiteDataStore.httpCookieStore.setCookie(cookie) {
            webView.load(URLRequest(url: url))
        }
    }

    private func beginNativeCommands(for sidecar: Sidecar) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                while let line = try Self.readNativeLine(sidecar.output.fileHandleForReading) {
                    guard let request = try? JSONDecoder().decode(NativeRequest.self, from: line),
                          request.v == 1, request.id.count == 32 else {
                        break
                    }
                    DispatchQueue.main.async { self?.handleNativeRequest(request) }
                }
            } catch {
                // The sidecar's pipe protocol is no longer usable. The process
                // will exit on its own; no native operation is performed.
            }
        }
    }

    private func handleNativeRequest(_ request: NativeRequest) {
        if request.op == "status", request.run_id == nil {
            reply(to: request.id, result: [
                "platform": "macos",
                "screen_recording_authorized": CGPreflightScreenCaptureAccess(),
                "accessibility_authorized": AXIsProcessTrusted(),
            ])
        } else if request.op == "select_window", let runID = request.run_id {
            selectWindow(for: runID, requestID: request.id)
        } else if request.op == "snapshot", let runID = request.run_id {
            snapshotWindow(for: runID, requestID: request.id)
        } else if ["press", "replace_text", "key"].contains(request.op),
                  let runID = request.run_id,
                  let args = request.args {
            performWindowAction(
                for: runID, requestID: request.id, operation: request.op, args: args
            )
        } else {
            reply(to: request.id, error: "unsupported native operation")
        }
    }

    private func selectWindow(for runID: String, requestID: String) {
        guard pendingPickerRequestID == nil else {
            reply(to: requestID, error: "window selection is already in progress")
            return
        }
        selectedWindow = nil
        selectedFilter = nil
        selectedRunID = nil
        lastCapture = nil
        activeCaptureRequestID = nil
        pendingPickerRequestID = requestID
        pendingPickerRunID = runID
        let picker = SCContentSharingPicker.shared
        if !pickerRegistered {
            picker.add(self)
            pickerRegistered = true
        }
        var configuration = SCContentSharingPickerConfiguration()
        configuration.allowedPickerModes = .singleWindow
        if let bundleID = Bundle.main.bundleIdentifier {
            configuration.excludedBundleIDs = [bundleID]
        }
        picker.configuration = configuration
        picker.isActive = true
        picker.present()
    }

    func contentSharingPicker(
        _ picker: SCContentSharingPicker,
        didUpdateWith filter: SCContentFilter,
        for stream: SCStream?
    ) {
        guard let requestID = pendingPickerRequestID,
              let runID = pendingPickerRunID else { return }
        pendingPickerRequestID = nil
        pendingPickerRunID = nil
        guard filter.includedWindows.count == 1,
              let window = filter.includedWindows.first,
              let application = window.owningApplication,
              application.bundleIdentifier != Bundle.main.bundleIdentifier else {
            reply(to: requestID, error: "select exactly one external app window")
            return
        }
        selectedWindow = window
        selectedFilter = filter
        selectedRunID = runID
        reply(to: requestID, result: [
            "window_id": Int(window.windowID),
            "bundle_id": application.bundleIdentifier,
            "process_id": Int(application.processID),
            "title": window.title ?? "",
        ])
    }

    func contentSharingPicker(
        _ picker: SCContentSharingPicker,
        didCancelFor stream: SCStream?
    ) {
        if let requestID = pendingPickerRequestID {
            pendingPickerRequestID = nil
            pendingPickerRunID = nil
            reply(to: requestID, error: "window selection was cancelled")
        }
    }

    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        if let requestID = pendingPickerRequestID {
            pendingPickerRequestID = nil
            pendingPickerRunID = nil
            reply(to: requestID, error: "window selection is unavailable")
        }
    }

    private func snapshotWindow(for runID: String, requestID: String) {
        guard selectedRunID == runID,
              let selectedWindow,
              let selectedFilter,
              let bundleID = selectedWindow.owningApplication?.bundleIdentifier else {
            reply(to: requestID, error: "select a window for this Run first")
            return
        }
        lastCapture = nil
        activeCaptureRequestID = requestID
        Task {
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(
                    false, onScreenWindowsOnly: true
                )
                guard let current = content.windows.first(where: {
                    $0.windowID == selectedWindow.windowID
                        && $0.owningApplication?.bundleIdentifier == bundleID
                        && $0.owningApplication?.processID
                            == selectedWindow.owningApplication?.processID
                }) else {
                    throw DesktopError.startupFailed
                }
                let configuration = SCStreamConfiguration()
                let width = max(1, Int(selectedFilter.contentRect.width
                    * CGFloat(selectedFilter.pointPixelScale)))
                let height = max(1, Int(selectedFilter.contentRect.height
                    * CGFloat(selectedFilter.pointPixelScale)))
                let scale = min(1.0, 2560.0 / Double(width), 1600.0 / Double(height))
                configuration.width = max(1, Int(Double(width) * scale))
                configuration.height = max(1, Int(Double(height) * scale))
                let processID = applicationProcessID(current)
                guard let beforeBounds = DesktopAccessibility.windowBounds(
                    windowID: current.windowID, processID: processID
                ) else {
                    throw DesktopError.startupFailed
                }
                let image = try await SCScreenshotManager.captureImage(
                    contentFilter: selectedFilter, configuration: configuration
                )
                guard let png = NSBitmapImageRep(cgImage: image).representation(
                    using: .png, properties: [:]
                ), png.count <= 12_000_000 else {
                    throw DesktopError.startupFailed
                }
                guard let bounds = DesktopAccessibility.windowBounds(
                    windowID: current.windowID,
                    processID: processID
                ), bounds == beforeBounds,
                   activeCaptureRequestID == requestID,
                   selectedRunID == runID,
                   self.selectedWindow?.windowID == current.windowID,
                   self.selectedWindow?.owningApplication?.processID == processID else {
                    throw DesktopError.startupFailed
                }
                let capture = NativeCapture(
                    runID: runID,
                    token: UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                    windowID: current.windowID,
                    processID: processID,
                    bundleID: bundleID,
                    title: current.title ?? "",
                    imageWidth: image.width,
                    imageHeight: image.height,
                    bounds: bounds,
                    capturedAt: Date()
                )
                activeCaptureRequestID = nil
                lastCapture = capture
                reply(to: requestID, result: [
                    "window_id": Int(current.windowID),
                    "bundle_id": bundleID,
                    "process_id": Int(current.owningApplication?.processID ?? 0),
                    "title": current.title ?? "",
                    "frame": [
                        "x": current.frame.origin.x,
                        "y": current.frame.origin.y,
                        "width": current.frame.width,
                        "height": current.frame.height,
                    ],
                    "image_base64": png.base64EncodedString(),
                    "capture_token": capture.token,
                ])
            } catch {
                if activeCaptureRequestID == requestID {
                    activeCaptureRequestID = nil
                }
                reply(to: requestID, error: "selected window capture is unavailable")
            }
        }
    }

    private func applicationProcessID(_ window: SCWindow) -> pid_t {
        window.owningApplication?.processID ?? 0
    }

    private func performWindowAction(
        for runID: String, requestID: String,
        operation: String, args: NativeArguments
    ) {
        guard selectedRunID == runID,
              let capture = lastCapture,
              capture.runID == runID,
              let token = args.capture_token, token == capture.token,
              let width = args.width, let height = args.height,
              width == capture.imageWidth, height == capture.imageHeight,
              Date().timeIntervalSince(capture.capturedAt) <= 120 else {
            reply(to: requestID, error: "a fresh matching window screenshot is required")
            return
        }
        if operation == "press" || operation == "replace_text" {
            guard let x = args.x, let y = args.y,
                  let label = args.expected_label,
                  x >= 0, y >= 0, x < width, y < height,
                  !label.isEmpty, label.count <= 120,
                  label == label.trimmingCharacters(in: .whitespacesAndNewlines) else {
                reply(to: requestID, error: "a labelled screenshot target is required")
                return
            }
            if operation == "replace_text" {
                guard let prior = args.expected_current_text,
                      let replacement = args.replacement_text,
                      prior.utf8.count <= 4096,
                      replacement.utf8.count <= 4096,
                      prior.utf8.count + replacement.utf8.count <= 4000,
                      !prior.unicodeScalars.contains(where: {
                          $0.value < 32 && $0 != "\n" && $0 != "\r" && $0 != "\t"
                      }),
                      !replacement.unicodeScalars.contains(where: {
                          $0.value < 32 && $0 != "\n" && $0 != "\r" && $0 != "\t"
                      }) else {
                    reply(to: requestID, error: "bounded current and replacement text are required")
                    return
                }
            }
        } else if operation == "key" {
            guard let title = args.expected_window_title,
                  title == capture.title, !title.isEmpty,
                  let key = args.key,
                  DesktopAccessibility.supportedKeys.contains(key) else {
                reply(to: requestID, error: "a supported key and exact window title are required")
                return
            }
        }
        // An error after dispatch may mean the target acted. Invalidate this
        // screenshot first so the same action cannot be replayed blindly.
        lastCapture = nil
        do {
            let result: [String: Any]
            if operation == "press", let x = args.x, let y = args.y,
               let label = args.expected_label {
                result = try DesktopAccessibility.press(
                    capture: capture, x: x, y: y, expectedLabel: label
                )
            } else if operation == "replace_text", let x = args.x,
                      let y = args.y, let label = args.expected_label,
                      let prior = args.expected_current_text,
                      let replacement = args.replacement_text {
                result = try DesktopAccessibility.replaceText(
                    capture: capture, x: x, y: y, expectedLabel: label,
                    expectedCurrent: prior, replacement: replacement
                )
            } else if operation == "key", let key = args.key {
                result = try DesktopAccessibility.sendKey(
                    capture: capture, key: key
                )
            } else {
                throw DesktopError.startupFailed
            }
            reply(to: requestID, result: result)
        } catch {
            reply(to: requestID, error: "selected window is not ready for this action")
        }
    }

    private func reply(
        to requestID: String,
        result: [String: Any]? = nil,
        error: String? = nil
    ) {
        guard let sidecar else { return }
        var message: [String: Any] = ["v": 1, "id": requestID, "ok": error == nil]
        if let error { message["error"] = error }
        else { message["result"] = result ?? [:] }
        guard let data = try? JSONSerialization.data(withJSONObject: message) else { return }
        sidecar.input.fileHandleForWriting.write(data + Data([10]))
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard navigationAction.targetFrame?.isMainFrame != false,
              let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        if let sidecar, url.scheme == "http", url.host == "127.0.0.1", url.port == sidecar.port {
            decisionHandler(.allow)
        } else {
            if navigationAction.navigationType == .linkActivated { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if !initialPageLoaded {
            initialPageLoaded = true
            fputs("OPHIRA desktop UI loaded\n", stderr)
            webView.callAsyncJavaScript(
                "return (await fetch('/api/desktop/status')).status",
                arguments: [:], in: nil, in: .page
            ) { result in
                switch result {
                case .success(let value):
                    let status = (value as? NSNumber)?.intValue ?? -1
                    if status == 200 {
                        fputs("OPHIRA native bridge ready\n", stderr)
                    } else {
                        fputs("OPHIRA native bridge HTTP \(status)\n", stderr)
                    }
                case .failure(let error):
                    fputs("OPHIRA native bridge request failed: \(error.localizedDescription)\n", stderr)
                }
            }
        }
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        if !initialPageLoaded && (error as NSError).code != NSURLErrorCancelled {
            showStartupError(error)
        }
    }

    private func showStartupError(_ error: Error) {
        let alert = NSAlert()
        alert.messageText = "OPHIRA could not start"
        alert.informativeText = error.localizedDescription
        alert.alertStyle = .critical
        alert.runModal()
        NSApp.terminate(nil)
    }
}

@main
@MainActor
private enum DesktopMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = DesktopApplication()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        application.run()
    }
}
