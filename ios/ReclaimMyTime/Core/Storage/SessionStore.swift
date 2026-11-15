import Foundation
import OSLog

/// What the app persists about a session. The token is redacted from every description so it
/// cannot reach logs by accident.
struct StoredSession: Codable, Hashable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    let token: String
    let sessionId: UUID
    let userId: UUID
    let expiresAt: Date

    init(token: String, sessionId: UUID, userId: UUID, expiresAt: Date) {
        self.token = token
        self.sessionId = sessionId
        self.userId = userId
        self.expiresAt = expiresAt
    }

    init(_ auth: AuthSession) {
        self.init(token: auth.token, sessionId: auth.session.id, userId: auth.user.id, expiresAt: auth.session.expiresAt)
    }

    var description: String { "StoredSession(session: \(sessionId), user: \(userId), token: <redacted>)" }
    var debugDescription: String { description }
}

/// Owns the opaque bearer session (Keychain-backed). The server is the authority on validity:
/// expiry is sliding, so the client never pre-emptively discards a session; a 401 does.
actor SessionStore {
    static let defaultService = "com.reclaimmytime.app.session"
    private static let account = "current"

    private let keychain: KeychainStore
    private var cached: StoredSession?
    private var loaded = false
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "session")

    init(keychain: KeychainStore = KeychainStore(service: SessionStore.defaultService)) {
        self.keychain = keychain
    }

    func current() -> StoredSession? {
        if !loaded {
            loaded = true
            do {
                if let data = try keychain.read(account: Self.account) {
                    cached = try JSONDecoder().decode(StoredSession.self, from: data)
                }
            } catch {
                logger.error("Failed to read stored session: \(String(describing: error), privacy: .public)")
                cached = nil
            }
        }
        return cached
    }

    func token() -> String? {
        current()?.token
    }

    @discardableResult
    func save(_ auth: AuthSession) throws -> StoredSession {
        let session = StoredSession(auth)
        try keychain.write(try JSONEncoder().encode(session), account: Self.account)
        cached = session
        loaded = true
        return session
    }

    func clear() {
        do {
            try keychain.delete(account: Self.account)
        } catch {
            logger.error("Failed to delete stored session: \(String(describing: error), privacy: .public)")
        }
        cached = nil
        loaded = true
    }
}
