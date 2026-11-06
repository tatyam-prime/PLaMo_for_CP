import AppKit
import Darwin
import Foundation

private let applicationID = "plamo-for-cp"
private let setupURL = URL(string: "http://127.0.0.1:8765/")!

final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var statusLabel: NSTextField!
    private var openButton: NSButton!
    private var textButton: NSButton!
    private var backend: Process?
    private var logSink: BoundedLog?
    private var outputPipe: Pipe?
    private var quitting = false
    private var ready = false
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".cache/plamo-cp/logs/desktop.log")

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let iconURL = Bundle.main.url(forResource: "app-icon", withExtension: "icns"),
           let icon = NSImage(contentsOf: iconURL) {
            NSApplication.shared.applicationIconImage = icon
        }
        buildWindow()
        inspectExistingService()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 300),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "PLaMo for CP"
        window.isReleasedWhenClosed = false
        window.center()
        let container = NSStackView()
        container.orientation = .vertical
        container.alignment = .leading
        container.spacing = 18
        container.translatesAutoresizingMaskIntoConstraints = false
        let title = NSTextField(labelWithString: "PLaMo for CP")
        title.font = .systemFont(ofSize: 25, weight: .bold)
        let description = NSTextField(wrappingLabelWithString:
            "翻訳を、このMacで実行します。\n標準は2B。モデルや保存先は管理画面で変更できます。")
        description.font = .systemFont(ofSize: 14)
        statusLabel = NSTextField(wrappingLabelWithString: "起動しています…")
        statusLabel.textColor = .secondaryLabelColor
        openButton = NSButton(title: "管理画面を開く", target: self, action: #selector(openSetup))
        openButton.isEnabled = false
        openButton.bezelStyle = .rounded
        openButton.keyEquivalent = "\r"
        textButton = NSButton(title: "テキストを翻訳", target: self, action: #selector(openText))
        textButton.isEnabled = false
        textButton.bezelStyle = .rounded
        let quit = NSButton(title: "終了", target: self, action: #selector(quitApplication))
        quit.bezelStyle = .rounded
        let logs = NSButton(title: "ログを表示", target: self, action: #selector(showLogs))
        logs.bezelStyle = .rounded
        let buttons = NSStackView(views: [openButton, textButton, logs, quit])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        let footnote = NSTextField(wrappingLabelWithString:
            "翻訳中はこのアプリを起動しておいてください。\n終了すると、このアプリが開始した翻訳処理を停止します。")
        footnote.font = .systemFont(ofSize: 11)
        footnote.textColor = .secondaryLabelColor
        for view in [title, description, statusLabel!, buttons, footnote] { container.addArrangedSubview(view) }
        window.contentView!.addSubview(container)
        NSLayoutConstraint.activate([
            container.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 26),
            container.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -26),
            container.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 26),
            description.widthAnchor.constraint(equalTo: container.widthAnchor),
            statusLabel.widthAnchor.constraint(equalTo: container.widthAnchor),
            footnote.widthAnchor.constraint(equalTo: container.widthAnchor)
        ])
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    private func probe(_ callback: @escaping (Bool, Bool) -> Void) {
        HealthProbe(port: setupURL.port ?? 8765).start { ours, occupied in
            DispatchQueue.main.async { callback(ours, occupied) }
        }
    }

    private func inspectExistingService() {
        probe { ours, occupied in
            if ours { self.markReady(existing: true) }
            else if occupied { self.fail("別のアプリが通信先を使用しています。ほかのPLaMo for CPを終了して、もう一度開いてください。") }
            else { self.startBackend() }
        }
    }

    private func startBackend() {
        guard let executable = Bundle.main.resourceURL?
            .appendingPathComponent("backend/plamo-cp-backend") else {
            fail("アプリの実行ファイルが見つかりません。もう一度インストールしてください。")
            return
        }
        do {
            let log = try BoundedLog(url: logURL)
            logSink = log
            let output = Pipe()
            outputPipe = output
            output.fileHandleForReading.readabilityHandler = { reader in
                let data = reader.availableData
                if data.isEmpty { reader.readabilityHandler = nil }
                else { try? log.append(data) }
            }
            let process = Process()
            process.executableURL = executable
            process.arguments = []
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            process.environment = environment
            process.standardOutput = output
            process.standardError = output
            process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
            process.terminationHandler = { process in
                DispatchQueue.main.async {
                    if !self.quitting {
                        self.ready = false
                        self.openButton.isEnabled = false
                        self.textButton.isEnabled = false
                        self.fail("翻訳アプリを起動できませんでした。［ログを表示］で詳しい状態を確認できます。")
                    }
                }
            }
            backend = process
            try process.run()
            try? output.fileHandleForWriting.close()
            waitUntilReady(attempt: 0)
        } catch {
            fail("翻訳アプリを起動できませんでした。ログの保存先やアプリの配置を確認してください。")
        }
    }

    private func waitUntilReady(attempt: Int) {
        guard !quitting else { return }
        if let child = backend, !child.isRunning { return }
        probe { ours, _ in
            if let child = self.backend, !child.isRunning { return }
            if ours { self.markReady(existing: false); return }
            if attempt >= 40 { self.fail("起動に時間がかかっています。［ログを表示］で状態を確認してください。"); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.waitUntilReady(attempt: attempt + 1) }
        }
    }

    private func markReady(existing: Bool) {
        ready = true
        statusLabel.stringValue = existing ? "起動済みのPLaMo for CPに接続しました。" : "起動しました。管理画面でモデルや保存先を確認できます。"
        statusLabel.textColor = .secondaryLabelColor
        openButton.isEnabled = true
        textButton.isEnabled = true
        openSetup()
    }

    private func fail(_ message: String) {
        statusLabel.stringValue = message
        statusLabel.textColor = .systemRed
    }

    private func openBrowser(_ url: URL) { if ready { NSWorkspace.shared.open(url) } }
    @objc private func openSetup() { openBrowser(setupURL) }
    @objc private func openText() { openBrowser(setupURL.appendingPathComponent("text")) }
    @objc private func showLogs() { NSWorkspace.shared.activateFileViewerSelecting([logURL]) }
    @objc private func quitApplication() { NSApplication.shared.terminate(nil) }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        if let child = backend, child.isRunning {
            child.interrupt()
            let deadline = Date().addingTimeInterval(8)
            while child.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
        if let pipe = outputPipe {
            try? pipe.fileHandleForWriting.close()
            pipe.fileHandleForReading.readabilityHandler = nil
            if let remaining = try? pipe.fileHandleForReading.readToEnd(), !remaining.isEmpty {
                try? logSink?.append(remaining)
            }
            try? pipe.fileHandleForReading.close()
        }
        logSink?.close()
    }
}

@main
struct ApplicationMain {
    static func main() {
        if CommandLine.arguments.contains("--check-bundle") {
            let executable = Bundle.main.resourceURL?.appendingPathComponent("backend/plamo-cp-backend")
            guard let executable = executable,
                  FileManager.default.isExecutableFile(atPath: executable.path) else {
                fputs("The bundled translation runtime is missing.\n", stderr)
                exit(1)
            }
            guard let icon = Bundle.main.url(forResource: "app-icon", withExtension: "icns"),
                  NSImage(contentsOf: icon) != nil else {
                fputs("The bundled application icon is missing or invalid.\n", stderr)
                exit(1)
            }
            let result: [String: Any] = [
                "bundle": Bundle.main.bundlePath,
                "backend": executable.path,
                "minimum_macos": Bundle.main.infoDictionary?["LSMinimumSystemVersion"] as? String ?? "unknown",
                "icon": icon.path
            ]
            if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
               let text = String(data: data, encoding: .utf8) { print(text) }
            return
        }
        let application = NSApplication.shared
        let delegate = ApplicationDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { application.run() }
    }
}
