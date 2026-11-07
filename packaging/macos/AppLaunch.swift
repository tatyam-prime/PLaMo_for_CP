import Foundation

enum AppLaunchProtocol {
    static let openURL = "plamo-for-cp://open"

    static func accepts(_ urls: [URL]) -> Bool {
        urls.count == 1 && urls[0].absoluteString == openURL
    }
}
