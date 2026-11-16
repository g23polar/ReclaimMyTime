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

/// `OutboxCounts`: write-back state of live blocks in the current and future weeks after the drain.
/// Both 0 when Google Calendar is not connected.
struct OutboxCounts: Codable, Hashable, Sendable {
    /// Not yet written to Google (retried automatically).
    let pending: Int
    let synced: Int
}

/// `GcalSyncResult`: the `POST /gcal/sync` answer (D25).
struct GcalSyncResult: Codable, Hashable, Sendable {
    /// The user's current pending changeset, whatever its reason (at most one, D4), else nil.
    let pendingChangeset: Changeset?
    let outbox: OutboxCounts

    private enum CodingKeys: String, CodingKey {
        case outbox
        case pendingChangeset = "pending_changeset"
    }

    init(pendingChangeset: Changeset?, outbox: OutboxCounts) {
        self.pendingChangeset = pendingChangeset
        self.outbox = outbox
    }
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
    var state: String? { GcalCallbackResult.stateParameter(of: authorizationUrl) }
}

/// The app-side result of `GET /gcal/callback`'s 302 into the app. Two routes share the shape:
/// - connect (T4.1): `reclaimmytime://gcal/callback?…`, finished by `POST /gcal/complete`;
/// - sign-in (T6.0): `reclaimmytime://auth/google/callback?…`, finished by
///   `POST /auth/google/complete`.
/// Query: `result=authorized&auth_code=…&state=…` (the app checks `state` against the URL it
/// opened before calling the server), or `result=error&code=…` (`gcal_access_denied`,
/// `gcal_exchange_failed`, `gcal_state_invalid`).
/// The callback route itself is hit by the browser, not the API client. The URL is only ever
/// received inside the app's own `ASWebAuthenticationSession` (see `GcalWebAuthenticator`).
enum GcalCallbackResult: Hashable, Sendable {
    case authorized(authCode: String, state: String)
    case error(code: String)

    /// Which flow a callback URL belongs to. A URL for one route never parses as the other.
    enum Route: Hashable, Sendable {
        case connect
        case signIn

        var host: String {
            switch self {
            case .connect: "gcal"
            case .signIn: "auth"
            }
        }

        var path: String {
            switch self {
            case .connect: "/callback"
            case .signIn: "/google/callback"
            }
        }
    }

    static let scheme = "reclaimmytime"
    static let stateInvalidCode = "gcal_state_invalid"

    /// A connect-flow callback (see `init(url:route:)`).
    init?(url: URL) {
        self.init(url: url, route: .connect)
    }

    /// Nil for URLs that aren't a callback for `route`, an unknown `result` (including the retired
    /// `connected`), or an `error` without a code. An `authorized` callback missing `auth_code` or
    /// `state` is `gcal_state_invalid`: it can't be completed and must not reach the server.
    init?(url: URL, route: Route) {
        guard url.scheme == GcalCallbackResult.scheme, url.host() == route.host, url.path() == route.path,
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

    /// The `state` query parameter of an authorization URL (nil if absent).
    static func stateParameter(of authorizationURL: URL) -> String? {
        URLComponents(url: authorizationURL, resolvingAgainstBaseURL: false)?
            .queryItems?.first(where: { $0.name == "state" })?.value
    }
}
