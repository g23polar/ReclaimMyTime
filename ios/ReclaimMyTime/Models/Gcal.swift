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

/// The app-side result of `GET /gcal/callback`'s 302 into the app
/// (`reclaimmytime://gcal/callback?result=connected|error&code=…`). The callback route itself is hit
/// by the browser, not the API client.
// TODO(T5.2/contract D15): the `reclaimmytime` scheme is a placeholder until a bundle scheme or
// universal link is chosen.
enum GcalCallbackResult: Hashable, Sendable {
    case connected
    case error(code: String)

    static let scheme = "reclaimmytime"

    init?(url: URL) {
        guard url.scheme == GcalCallbackResult.scheme, url.host() == "gcal", url.path() == "/callback",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let result = items.first(where: { $0.name == "result" })?.value
        else { return nil }
        switch result {
        case "connected":
            self = .connected
        case "error":
            guard let code = items.first(where: { $0.name == "code" })?.value, !code.isEmpty else { return nil }
            self = .error(code: code)
        default:
            return nil
        }
    }
}
