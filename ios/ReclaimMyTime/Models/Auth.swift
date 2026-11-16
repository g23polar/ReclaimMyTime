import Foundation

/// `AppleSignInRequest`: body of `POST /auth/apple`. `nonce` is the RAW nonce; Apple received its
/// SHA-256 hex digest.
struct AppleSignInRequest: Codable, Hashable, Sendable {
    let identityToken: String
    let authorizationCode: String
    let nonce: String

    private enum CodingKeys: String, CodingKey {
        case identityToken = "identity_token"
        case authorizationCode = "authorization_code"
        case nonce
    }
}

struct User: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    /// Null unless Apple reported a verified, non-private-relay address.
    let email: String?
    let createdAt: Date

    private enum CodingKeys: String, CodingKey {
        case id, email
        case createdAt = "created_at"
    }
}

/// `Session` schema (server-side session row, no secrets).
struct Session: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let createdAt: Date
    let expiresAt: Date

    private enum CodingKeys: String, CodingKey {
        case id
        case createdAt = "created_at"
        case expiresAt = "expires_at"
    }
}

/// `AuthSession`: response of `POST /auth/apple`. `token` is shown once; it goes straight to the
/// Keychain and must never be logged (`description` is redacted).
struct AuthSession: Codable, Hashable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    let token: String
    let session: Session
    let user: User
    let isNewUser: Bool

    private enum CodingKeys: String, CodingKey {
        case token, session, user
        case isNewUser = "is_new_user"
    }

    var description: String { "AuthSession(session: \(session.id), user: \(user.id), token: <redacted>)" }
    var debugDescription: String { description }
}

// MARK: Google sign-in (T6.0)

/// `POST /auth/google/start` body. `nonceHash` is `Nonce.sha256Base64URL(raw)`; the raw nonce only
/// leaves the app later, in `GoogleSignInCompleteRequest`.
struct GoogleSignInStartRequest: Codable, Hashable, Sendable {
    let nonceHash: String

    private enum CodingKeys: String, CodingKey { case nonceHash = "nonce_hash" }
}

/// `POST /auth/google/start` response: open `authorizationUrl` in ASWebAuthenticationSession.
struct GoogleSignInStart: Codable, Hashable, Sendable {
    let authorizationUrl: URL

    private enum CodingKeys: String, CodingKey { case authorizationUrl = "authorization_url" }

    /// The callback's `state` must equal this before the app calls `POST /auth/google/complete`.
    var state: String? { GcalCallbackResult.stateParameter(of: authorizationUrl) }
}

/// `POST /auth/google/complete` body: the `authorized` callback's `auth_code`/`state` plus the RAW
/// nonce whose hash went to `/auth/google/start`.
struct GoogleSignInCompleteRequest: Codable, Hashable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    let authCode: String
    let state: String
    let nonce: String

    private enum CodingKeys: String, CodingKey {
        case state, nonce
        case authCode = "auth_code"
    }

    var description: String { "GoogleSignInCompleteRequest(<redacted>)" }
    var debugDescription: String { description }
}

/// `POST /auth/google/complete` response: exactly the `AuthSession` schema (as from
/// `POST /auth/apple`) plus a required `gcal_connected`. True when the same Google consent also
/// granted the calendar scopes and the connection is active: onboarding's "Connect Google
/// Calendar" step is skipped. False (calendar scopes unticked): the step stays.
struct GoogleSignInResult: Decodable, Hashable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    let auth: AuthSession
    let gcalConnected: Bool

    init(auth: AuthSession, gcalConnected: Bool) {
        self.auth = auth
        self.gcalConnected = gcalConnected
    }

    private enum CodingKeys: String, CodingKey {
        case gcalConnected = "gcal_connected"
    }

    /// The `AuthSession` fields decode from the same flat object; `gcal_connected` is required.
    init(from decoder: any Decoder) throws {
        auth = try AuthSession(from: decoder)
        gcalConnected = try decoder.container(keyedBy: CodingKeys.self).decode(Bool.self, forKey: .gcalConnected)
    }

    var description: String { "GoogleSignInResult(\(auth), gcalConnected: \(gcalConnected))" }
    var debugDescription: String { description }
}
