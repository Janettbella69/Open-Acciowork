import AppKit
import Darwin
import Foundation
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
    let port: Int
    let token: String
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
private final class DesktopApplication: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var sidecar: Sidecar?
    private var window: NSWindow?
    private var webView: WKWebView?
    private var initialPageLoaded = false

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
                        process: process, input: input, port: handshake.port, token: token
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

MainActor.assumeIsolated {
    let application = NSApplication.shared
    let delegate = DesktopApplication()
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    application.run()
}
