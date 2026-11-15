import Foundation

/// Mock-mode stand-in for the Google OAuth browser hop: returns the callback the
/// `get_gcal_callback__connected` fixture redirects to. Launch with `-MockGcalResult <code>` to
/// exercise an error callout instead (e.g. `gcal_access_denied`).
enum MockWebAuthenticator {
    static func callbackURL(arguments: [String] = ProcessInfo.processInfo.arguments) -> URL {
        if let flag = arguments.firstIndex(of: "-MockGcalResult"), arguments.indices.contains(flag + 1) {
            return URL(string: "\(GcalCallbackResult.scheme)://gcal/callback?result=error&code=\(arguments[flag + 1])")!
        }
        return URL(string: "\(GcalCallbackResult.scheme)://gcal/callback?result=connected")!
    }
}
