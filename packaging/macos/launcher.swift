import AppKit
import CoreServices
import Darwin
import Foundation

private let applicationID = "plamo-for-cp"
private let setupURL = URL(string: "http://127.0.0.1:8765/")!

final class ApplicationDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var statusLabel: NSTextField!
    private var openButton: NSButton!
    private var textButton: NSButton!
    private var modelValue: NSTextField!
    private var utilizationValue: NSTextField!
    private var contentStack: NSStackView!
    private var settingsRequest: DesktopSettingsRequest?
    private var settingsTimer: Timer?
    private var settingsEpoch = 0
    private var backend: Process?
    private var logSink: BoundedLog?
    private var outputPipe: Pipe?
    private var quitting = false
    private var ready = false
    private var protocolLaunch = false
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".cache/plamo-cp/logs/desktop.log")

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Register this normal .app launch, not a developer staging build step.
        // LaunchServices handles the user's browser consent and app selection.
        if Bundle.main.bundleURL.pathExtension == "app" {
            LSRegisterURL(Bundle.main.bundleURL as CFURL, true)
        }
        if let iconURL = Bundle.main.url(forResource: "app-icon", withExtension: "icns"),
           let icon = NSImage(contentsOf: iconURL) {
            NSApplication.shared.applicationIconImage = icon
        }
        buildWindow()
        inspectExistingService()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        bringToForeground()
        return true
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard AppLaunchProtocol.accepts(urls) else { return }
        protocolLaunch = true
        // A cold-launch URL event can precede applicationDidFinishLaunching.
        // buildWindow() foregrounds the window later when it does not exist yet.
        bringToForeground()
    }

    private func bringToForeground() {
        guard let window = window else { return }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        refreshSettings()
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 180),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "PLaMo for 競プロ"
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        let container = NSStackView()
        contentStack = container
        container.orientation = .vertical
        container.alignment = .leading
        container.spacing = 12
        container.detachesHiddenViews = true
        container.translatesAutoresizingMaskIntoConstraints = false
        let title = NSTextField(labelWithString: "PLaMo for 競プロ")
        title.font = .systemFont(ofSize: 20, weight: .bold)
        func valueField() -> NSTextField {
            let value = NSTextField(labelWithString: "—")
            value.font = .systemFont(ofSize: 13)
            value.isSelectable = true
            value.maximumNumberOfLines = 1
            value.lineBreakMode = .byTruncatingMiddle
            value.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            value.setContentHuggingPriority(.defaultLow, for: .horizontal)
            return value
        }
        modelValue = valueField()
        utilizationValue = valueField()
        let rows: [(String, NSTextField)] = [("読込中のモデル", modelValue), ("稼働率", utilizationValue)]
        let settings = NSGridView(views: rows.map { name, value in
            let label = NSTextField(labelWithString: name)
            label.font = .systemFont(ofSize: 12)
            label.textColor = .secondaryLabelColor
            return [label, value]
        })
        settings.rowSpacing = 8
        settings.columnSpacing = 12
        settings.column(at: 0).width = 96
        settings.column(at: 1).xPlacement = .fill
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
        buttons.spacing = 6
        for view in [title, settings, statusLabel!, buttons] { container.addArrangedSubview(view) }
        window.contentView!.addSubview(container)
        NSLayoutConstraint.activate([
            container.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 18),
            container.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -18),
            container.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 18),
            settings.widthAnchor.constraint(equalTo: container.widthAnchor),
            statusLabel.widthAnchor.constraint(equalTo: container.widthAnchor),
            container.bottomAnchor.constraint(lessThanOrEqualTo: window.contentView!.bottomAnchor, constant: -14)
        ])
        fitWindowToContent()
        bringToForeground()
    }

    private func fitWindowToContent() {
        guard let window = window, let stack = contentStack else { return }
        window.contentView?.layoutSubtreeIfNeeded()
        let height = max(160, ceil(stack.fittingSize.height) + 32)
        guard abs((window.contentView?.frame.height ?? 0) - height) > 0.5 else { return }
        let previous = window.frame
        var frame = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: 420, height: height))
        frame.origin = NSPoint(x: previous.minX, y: previous.maxY - frame.height)
        window.setFrame(frame, display: true)
    }

    private func probe(_ callback: @escaping (Bool, Bool) -> Void) {
        HealthProbe(port: setupURL.port ?? 8765).start { ours, occupied in
            DispatchQueue.main.async { callback(ours, occupied) }
        }
    }

    private func inspectExistingService() {
        probe { ours, occupied in
            if ours { self.markReady(existing: true) }
            else if occupied { self.fail("別のアプリが通信先を使用しています。ほかのPLaMo for 競プロを終了して、もう一度開いてください。") }
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
        statusLabel.stringValue = "設定を確認しています…"
        statusLabel.isHidden = false
        fitWindowToContent()
        statusLabel.textColor = .secondaryLabelColor
        openButton.isEnabled = true
        textButton.isEnabled = true
        refreshSettings()
        if !protocolLaunch { openSetup() }
    }

    private func fail(_ message: String) {
        ready = false
        pauseSettingsRefresh()
        openButton.isEnabled = false
        textButton.isEnabled = false
        statusLabel.stringValue = message
        statusLabel.isHidden = false
        statusLabel.textColor = .systemRed
        fitWindowToContent()
    }

    private var settingsVisible: Bool {
        ready && !quitting && window?.isVisible == true && window?.isMiniaturized == false && !NSApplication.shared.isHidden
    }

    private func pauseSettingsRefresh() {
        settingsTimer?.invalidate()
        settingsTimer = nil
        settingsEpoch += 1
        settingsRequest?.cancel()
        settingsRequest = nil
    }

    private func refreshSettings() {
        guard settingsVisible else { pauseSettingsRefresh(); return }
        guard settingsRequest == nil else { return }
        settingsTimer?.invalidate()
        settingsTimer = nil
        settingsEpoch += 1
        let epoch = settingsEpoch
        let request = DesktopSettingsRequest(port: setupURL.port ?? 8765)
        settingsRequest = request
        request.start { [weak self] snapshot in
            DispatchQueue.main.async {
                guard let self = self, epoch == self.settingsEpoch, !self.quitting else { return }
                self.settingsRequest = nil
                guard self.settingsVisible else { self.pauseSettingsRefresh(); return }
                if let snapshot = snapshot {
                    self.modelValue.stringValue = snapshot.model
                    self.utilizationValue.stringValue = snapshot.utilization
                    self.statusLabel.stringValue = ""
                    self.statusLabel.isHidden = true
                } else {
                    self.statusLabel.stringValue = "設定を取得できません。再接続しています…"
                    self.statusLabel.isHidden = false
                    self.statusLabel.textColor = .systemOrange
                }
                self.fitWindowToContent()
                self.settingsTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { [weak self] _ in
                    self?.refreshSettings()
                }
            }
        }
    }

    func windowDidBecomeKey(_ notification: Notification) { refreshSettings() }
    func windowDidDeminiaturize(_ notification: Notification) { refreshSettings() }
    func windowDidMiniaturize(_ notification: Notification) { pauseSettingsRefresh() }
    func windowWillClose(_ notification: Notification) { pauseSettingsRefresh() }
    func applicationDidHide(_ notification: Notification) { pauseSettingsRefresh() }
    func applicationDidUnhide(_ notification: Notification) { refreshSettings() }

    private func openBrowser(_ url: URL) { if ready { NSWorkspace.shared.open(url) } }
    @objc private func openSetup() { openBrowser(setupURL) }
    @objc private func openText() { openBrowser(setupURL.appendingPathComponent("text")) }
    @objc private func showLogs() { NSWorkspace.shared.activateFileViewerSelecting([logURL]) }
    @objc private func quitApplication() { NSApplication.shared.terminate(nil) }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        pauseSettingsRefresh()
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
            let urlTypes = Bundle.main.infoDictionary?["CFBundleURLTypes"] as? [[String: Any]] ?? []
            guard urlTypes.contains(where: {
                ($0["CFBundleURLSchemes"] as? [String]) == ["plamo-for-cp"]
            }) else {
                fputs("The fixed application launch protocol is missing.\n", stderr)
                exit(1)
            }
            let result: [String: Any] = [
                "bundle": Bundle.main.bundlePath,
                "backend": executable.path,
                "minimum_macos": Bundle.main.infoDictionary?["LSMinimumSystemVersion"] as? String ?? "unknown",
                "icon": icon.path,
                "launch_url": AppLaunchProtocol.openURL
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
