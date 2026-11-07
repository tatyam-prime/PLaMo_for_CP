import CoreFoundation
import Foundation

struct DesktopSettingsSnapshot {
    let model: String
    let utilization: String

    init?(health: [String: Any], setup: [String: Any]) {
        guard health["app"] as? String == "plamo-for-cp",
              setup["application"] as? String == "plamo-for-cp" else { return nil }
        let engine = health["engine"] as? [String: Any] ?? [:]
        let names = ["0.8b": "800M（0.8B）", "2b": "2B", "8b": "8B"]
        let loaded = engine["loaded_model"]
        if loaded == nil || loaded is NSNull { model = "なし" }
        else if let id = loaded as? String { model = names[id] ?? "—" }
        else { model = "—" }
        let percentages = [100, 80, 64, 50, 40, 32, 25, 20]
        if let level = Self.integer(setup["quiet_level"]), percentages.indices.contains(level) {
            utilization = "\(percentages[level])%"
        } else if let legacy = Self.boolean(setup["quiet_mode"]) {
            utilization = legacy ? "50%" : "100%"
        } else { utilization = "—" }
    }

    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    private static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let numberValue = number.doubleValue
        guard numberValue.isFinite, numberValue >= 0, numberValue <= 7,
              numberValue.rounded() == numberValue else { return nil }
        return Int(numberValue)
    }
}

/// Fetch one complete settings snapshot from fixed loopback GET routes.
/// Its serial delegate queue owns every mutable field, including cancellation.
final class DesktopSettingsRequest: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    private let port: Int
    private let timeout: TimeInterval
    private let maxBytes: Int
    private let queue = OperationQueue()
    private let paths = ["/api/health", "/api/setup"]
    private var index = 0
    private var body = Data()
    private var response: HTTPURLResponse?
    private var values = [String: [String: Any]]()
    private var session: URLSession?
    private var completion: ((DesktopSettingsSnapshot?) -> Void)?
    private var started = false
    private var finished = false
    private var rejected = false

    init(port: Int = 44894, timeout: TimeInterval = 1.5, maxBytes: Int = 65536) {
        self.port = port
        self.timeout = max(0.05, min(timeout, 5))
        self.maxBytes = max(1, min(maxBytes, 1048576))
        super.init()
        queue.maxConcurrentOperationCount = 1
    }

    func start(_ completion: @escaping (DesktopSettingsSnapshot?) -> Void) {
        queue.addOperation {
            guard !self.started, !self.finished else { completion(nil); return }
            self.started = true
            self.completion = completion
            let configuration = URLSessionConfiguration.ephemeral
            configuration.connectionProxyDictionary = [:]
            configuration.timeoutIntervalForRequest = self.timeout
            configuration.timeoutIntervalForResource = self.timeout
            configuration.urlCache = nil
            self.session = URLSession(configuration: configuration, delegate: self, delegateQueue: self.queue)
            self.next()
        }
    }

    func cancel() { queue.addOperation { self.finish(nil) } }

    private func next() {
        guard !finished else { return }
        if index == paths.count {
            finish(DesktopSettingsSnapshot(health: values[paths[0]]!, setup: values[paths[1]]!))
            return
        }
        guard (1...65535).contains(port), let url = URL(string: "http://127.0.0.1:\(port)\(paths[index])") else {
            finish(nil); return
        }
        body.removeAll(keepingCapacity: true)
        response = nil
        rejected = false
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = "GET"
        session?.dataTask(with: request).resume()
    }

    private func finish(_ snapshot: DesktopSettingsSnapshot?) {
        guard !finished else { return }
        finished = true
        let callback = completion
        completion = nil
        session?.invalidateAndCancel()
        session = nil
        callback?(snapshot)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        rejected = true
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        self.response = response as? HTTPURLResponse
        guard !finished, self.response?.statusCode == 200,
              response.expectedContentLength <= Int64(maxBytes) else {
            rejected = true; completionHandler(.cancel); return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard !finished, !rejected else { return }
        if data.count > maxBytes - body.count {
            rejected = true; body.removeAll(); dataTask.cancel()
        } else { body.append(data) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard !finished else { return }
        guard error == nil, !rejected, response?.statusCode == 200,
              let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else {
            finish(nil); return
        }
        if index == 0 && object["app"] as? String != "plamo-for-cp" { finish(nil); return }
        if index == 1 && object["application"] as? String != "plamo-for-cp" { finish(nil); return }
        values[paths[index]] = object
        index += 1
        next()
    }
}
