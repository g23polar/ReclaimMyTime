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
