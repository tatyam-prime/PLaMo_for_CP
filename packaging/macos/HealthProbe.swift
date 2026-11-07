import Foundation

/// A bounded loopback check. Redirects and unrelated listeners are never trusted.
final class HealthProbe: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    private let port: Int
    private let timeout: TimeInterval
    private let maxBytes: Int
    private var session: URLSession?
    private var completion: ((Bool, Bool) -> Void)?
    private var response: HTTPURLResponse?
    private var receivedResponse = false
    private var rejected = false
    private var body = Data()

    init(port: Int = 44894, timeout: TimeInterval = 1, maxBytes: Int = 65536) {
        self.port = port
        self.timeout = timeout
        self.maxBytes = maxBytes
    }

    func start(_ completion: @escaping (Bool, Bool) -> Void) {
        self.completion = completion
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = timeout
        configuration.timeoutIntervalForResource = timeout
        configuration.urlCache = nil
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        self.session = session
        session.dataTask(with: URL(string: "http://127.0.0.1:\(port)/api/health")!).resume()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        receivedResponse = true
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        receivedResponse = true
        self.response = response as? HTTPURLResponse
        if response.expectedContentLength > Int64(maxBytes) {
            rejected = true
            completionHandler(.cancel)
        } else {
            completionHandler(.allow)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard !rejected else { return }
        if data.count > maxBytes - body.count {
            rejected = true
            body.removeAll()
            dataTask.cancel()
        } else {
            body.append(data)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
        let ours = error == nil && !rejected && response?.statusCode == 200 &&
            object?["app"] as? String == "plamo-for-cp"
        let timedOut = (error as? URLError)?.code == .timedOut
        let callback = completion
        completion = nil
        session.finishTasksAndInvalidate()
        self.session = nil
        callback?(ours, receivedResponse || timedOut)
    }
}
