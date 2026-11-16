import Foundation

/// Mock-mode stand-in for the Google OAuth browser hop. By default it returns the `authorized`
/// callback the server would redirect to, with the `state` of the `get_gcal_connect__ok` fixture's
/// `authorization_url`, so the app's client-side state check passes and it goes on to
/// `POST /gcal/complete` (served by `MockTransport`). Launch with `-MockGcalResult <code>` to
/// exercise an error callout instead (e.g. `gcal_access_denied`).
enum MockWebAuthenticator {
    static let fixtureAuthCode = "4/0AeanS0aExampleAuthCode"
    static let connectFixtureName = "get_gcal_connect__ok.json"

    /// - Parameter authorizationURL: the URL the app opened; its `state` is the fallback when the
    ///   connect fixture can't be read (it is that fixture's URL in mock mode anyway).
    static func callbackURL(
        authorizationURL: URL,
        arguments: [String] = ProcessInfo.processInfo.arguments,
        fixtureState: String? = connectFixtureState()
    ) -> URL {
        var components = URLComponents()
        components.scheme = GcalCallbackResult.scheme
        components.host = "gcal"
        components.path = "/callback"
        if let flag = arguments.firstIndex(of: "-MockGcalResult"), arguments.indices.contains(flag + 1) {
            components.queryItems = [
                URLQueryItem(name: "result", value: "error"),
                URLQueryItem(name: "code", value: arguments[flag + 1]),
            ]
        } else {
            let state = fixtureState ?? GcalConnectUrl(authorizationUrl: authorizationURL).state ?? ""
            components.queryItems = [
                URLQueryItem(name: "result", value: "authorized"),
                URLQueryItem(name: "auth_code", value: fixtureAuthCode),
                URLQueryItem(name: "state", value: state),
            ]
        }
        // `URLQueryItem` leaves `+`, `/`, `=` unescaped; encode them so the URL matches what the
        // server sends (urlencoded values).
        components.percentEncodedQuery = components.percentEncodedQuery?
            .replacingOccurrences(of: "+", with: "%2B")
            .replacingOccurrences(of: "/", with: "%2F")
        return components.url!
    }

    /// `state` from the bundled `get_gcal_connect__ok` fixture's `authorization_url` (nil if absent).
    static func connectFixtureState(in bundle: Bundle = .main) -> String? {
        guard let directory = FixtureLoader.bundledDirectory(in: bundle),
              let fixture = try? FixtureLoader.parse(directory.appending(path: connectFixtureName)),
              let body = fixture.responseBody,
              let connect = try? JSONCoding.makeDecoder().decode(GcalConnectUrl.self, from: body)
        else { return nil }
        return connect.state
    }
}
