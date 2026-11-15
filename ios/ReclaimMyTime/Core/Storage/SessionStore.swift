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

/// The Keychain could not be read (e.g. device locked before first unlock). This is NOT "no
/// session": the store stays unloaded so the next call retries.
enum SessionStoreError: Error, Hashable, Sendable {
    case keychainUnavailable
}

/// Owns the opaque bearer session (Keychain-backed). The server is the authority on validity:
/// expiry is sliding, so the client never pre-emptively discards a session; a 401 does.
///
/// Each backend gets its own Keychain service (`forBackend`), so flipping `-UseMockAPI` never
/// reuses or deletes the other backend's credential.
actor SessionStore {
    static let defaultService = "com.reclaimmytime.app.session"
    static let account = "current"

    private let keychain: any KeychainStoring
    private var cached: StoredSession?
    private var loaded = false
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "session")

    init(keychain: any KeychainStoring = KeychainStore(service: SessionStore.defaultService)) {
        self.keychain = keychain
    }

    /// A store whose Keychain slot is keyed by backend identity.
    static func forBackend(_ backend: AppConfig.Backend, prefix: String = defaultService) -> SessionStore {
        SessionStore(keychain: KeychainStore(service: service(for: backend, prefix: prefix)))
    }

    /// `<prefix>.mock`, or `<prefix>.live.<host>[:<port>]` for a live base URL.
    static func service(for backend: AppConfig.Backend, prefix: String = defaultService) -> String {
        switch backend {
        case .mock:
            return "\(prefix).mock"
        case .live(let url):
            let host = url.host()?.lowercased() ?? "unknown"
            let port = url.port.map { ":\($0)" } ?? ""
            return "\(prefix).live.\(host)\(port)"
        }
    }

    /// The stored session, or nil when the Keychain confirms there is none.
    /// Throws `SessionStoreError.keychainUnavailable` when the read itself fails; nothing is cached
    /// then, so a later call retries.
    func current() throws -> StoredSession? {
        if loaded { return cached }
        let data: Data?
        do {
            data = try keychain.read(account: Self.account)
        } catch {
            logger.error("Failed to read stored session: \(String(describing: error), privacy: .public)")
            throw SessionStoreError.keychainUnavailable
        }
        if let data {
            do {
                cached = try JSONDecoder().decode(StoredSession.self, from: data)
            } catch {
                // Readable but undecodable: a corrupt item is unusable, so treat it as absent.
                logger.error("Stored session is undecodable; ignoring it")
                cached = nil
            }
        } else {
            cached = nil
        }
        loaded = true
        return cached
    }

    /// The bearer token for a request; nil when there is no session or the Keychain is unreadable.
    func token() -> String? {
        (try? current())?.token
    }

    @discardableResult
    func save(_ auth: AuthSession) throws -> StoredSession {
        let session = StoredSession(auth)
        try keychain.write(try JSONEncoder().encode(session), account: Self.account)
        cached = session
        loaded = true
        return session
    }

    /// Unconditional clear (sign-out).
    func clear() {
        do {
            try keychain.delete(account: Self.account)
        } catch {
            logger.error("Failed to delete stored session: \(String(describing: error), privacy: .public)")
        }
        cached = nil
        loaded = true
    }

    /// Compare-and-clear for 401 handling: clears only if the stored session is still the one whose
    /// token the failed request carried. Returns true when the caller should treat the session as
    /// invalidated (cleared now, or both sides already empty). A late 401 for an old session
    /// therefore never deletes a replacement, and an unreadable Keychain is left alone.
    func clear(ifToken expected: String?) -> Bool {
        let stored: StoredSession?
        do {
            stored = try current()
        } catch {
            return false
        }
        guard stored?.token == expected else { return false }
        if stored != nil { clear() }
        return true
    }
}
