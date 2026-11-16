import Foundation

struct GcalConnectUrl: Codable, Hashable, Sendable {
    /// Open in ASWebAuthenticationSession (D15).
    let authorizationUrl: URL

    private enum CodingKeys: String, CodingKey { case authorizationUrl = "authorization_url" }
}

/// `GoogleConnection`: connection row minus secrets.
struct GoogleConnection: Codable, Hashable, Sendable {
    let googleEmail: String
    let scopes: [String]
    let appCalendarId: String?
    let status: ConnectionStatus
    let revokedAt: Date?
    let createdAt: Date

    private enum CodingKeys: String, CodingKey {
        case scopes, status
        case googleEmail = "google_email"
        case appCalendarId = "app_calendar_id"
        case revokedAt = "revoked_at"
        case createdAt = "created_at"
    }
}

struct GcalStatus: Codable, Hashable, Sendable {
    /// Nil when never connected or after disconnect.
    let connection: GoogleConnection?
}

/// `POST /gcal/complete` body: the `auth_code`/`state` pair from an `authorized` callback.
struct GcalCompleteRequest: Codable, Hashable, Sendable {
    let authCode: String
    let state: String

    private enum CodingKeys: String, CodingKey {
        case state
        case authCode = "auth_code"
    }
}

extension GcalConnectUrl {
    /// The `state` query parameter of the authorization URL the app opens. The callback's `state`
    /// must equal it before the app calls `POST /gcal/complete`.
    var state: String? {
        URLComponents(url: authorizationUrl, resolvingAgainstBaseURL: false)?
            .queryItems?.first(where: { $0.name == "state" })?.value
    }
}

/// The app-side result of `GET /gcal/callback`'s 302 into the app:
/// - `reclaimmytime://gcal/callback?result=authorized&auth_code=…&state=…` — the app must finish
///   the connection with `POST /gcal/complete` (after checking `state` against the URL it opened);
/// - `reclaimmytime://gcal/callback?result=error&code=…` (`gcal_access_denied`,
///   `gcal_exchange_failed`, `gcal_state_invalid`).
/// The callback route itself is hit by the browser, not the API client. The URL is only ever
/// received inside the app's own `ASWebAuthenticationSession` (see `GcalWebAuthenticator`).
enum GcalCallbackResult: Hashable, Sendable {
    case authorized(authCode: String, state: String)
    case error(code: String)

    static let scheme = "reclaimmytime"
    static let stateInvalidCode = "gcal_state_invalid"

    /// Nil for URLs that aren't a gcal callback, an unknown `result` (including the retired
    /// `connected`), or an `error` without a code. An `authorized` callback missing `auth_code` or
    /// `state` is `gcal_state_invalid`: it can't be completed and must not reach the server.
    init?(url: URL) {
        guard url.scheme == GcalCallbackResult.scheme, url.host() == "gcal", url.path() == "/callback",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let result = items.first(where: { $0.name == "result" })?.value
        else { return nil }
        func value(_ name: String) -> String? {
            items.first(where: { $0.name == name })?.value.flatMap { $0.isEmpty ? nil : $0 }
        }
        switch result {
        case "authorized":
            guard let authCode = value("auth_code"), let state = value("state") else {
                self = .error(code: GcalCallbackResult.stateInvalidCode)
                return
            }
            self = .authorized(authCode: authCode, state: state)
        case "error":
            guard let code = value("code") else { return nil }
            self = .error(code: code)
        default:
            return nil
        }
    }
}
