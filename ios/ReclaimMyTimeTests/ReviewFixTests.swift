import XCTest
@testable import ReclaimMyTime

// Regression tests for the T5.1 code-review findings fixed in T5.2.

/// Holds every request until the test releases it, then answers 401 `unauthenticated`.
actor GatedUnauthorizedTransport: HTTPTransport {
    private var waiters: [CheckedContinuation<Void, Never>] = []
    /// Releases that arrived before their request did; consumed on arrival. Without this a
    /// release racing ahead of a slow request was dropped and the test hung forever.
    private var earlyReleases = 0
    private(set) var authorizationHeaders: [String?] = []

    var pendingCount: Int { waiters.count }

    func releaseNext() {
        guard !waiters.isEmpty else {
            earlyReleases += 1
            return
        }
        waiters.removeFirst().resume()
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        authorizationHeaders.append(request.value(forHTTPHeaderField: "Authorization"))
        if earlyReleases > 0 {
            earlyReleases -= 1
        } else {
            await withCheckedContinuation { waiters.append($0) }
        }
        let body = try JSONCoding.makeEncoder().encode(ErrorEnvelope(error: ErrorBody(code: "unauthenticated", message: "expired")))
        let response = HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        return (body, response)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        throw URLError(.unsupportedURL)
    }
}

/// Never answers (until cancelled): models a revoke call that hangs.
struct HangingTransport: HTTPTransport {
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        try await Task.sleep(for: .seconds(3600))
        throw URLError(.timedOut)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        try await Task.sleep(for: .seconds(3600))
        throw URLError(.timedOut)
    }
}

private func makeSession(token: String) throws -> AuthSession {
    let fixture = try TestFixtures.authSession()
    return AuthSession(token: token, session: fixture.session, user: fixture.user, isNewUser: false)
}

/// Polls a main-actor condition until it holds or the deadline passes.
@MainActor
private func waitUntil(timeout: Duration = .seconds(2), _ condition: () -> Bool) async -> Bool {
    let clock = ContinuousClock()
    let deadline = clock.now.advanced(by: timeout)
    while clock.now < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(10))
    }
    return condition()
}

// MARK: 1. A delayed 401 must not delete a replacement session

@MainActor
final class DelayedUnauthorizedTests: XCTestCase {
    func testLate401ForOldSessionDoesNotClearReplacementSession() async throws {
        let (store, _) = makeTestSessionStore()
        defer { Task { await store.clear() } }
        try await store.save(makeSession(token: "token-A"))

        let transport = GatedUnauthorizedTransport()
        let notifications = NotificationCounter()
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: transport, sessionStore: store,
                                      onUnauthenticated: { _ in await notifications.increment() })

        let a1 = Task { try? await api.getMe() }
        let a2 = Task { try? await api.getMe() }
        // Both requests have been built (with session A) and are in flight.
        for _ in 0..<1000 where await transport.pendingCount < 2 {  // up to 5 s under load
            try await Task.sleep(for: .milliseconds(5))
        }
        let pending = await transport.pendingCount
        XCTAssertEqual(pending, 2)
        let headers = await transport.authorizationHeaders
        XCTAssertEqual(headers, ["Bearer token-A", "Bearer token-A"])

        // `releaseNext` answers whichever request reached the transport first (a1 or a2: their
        // arrival order isn't guaranteed), so wait for that 401 to land rather than for a1.
        // Awaiting a1 here hung the suite whenever a2 arrived first (a1 was never released).
        await transport.releaseNext()
        for _ in 0..<1000 where await notifications.count < 1 {  // up to 5 s under load
            try await Task.sleep(for: .milliseconds(5))
        }
        let afterFirst = try await store.current()
        XCTAssertNil(afterFirst, "A1's 401 clears session A")
        let firstCount = await notifications.count
        XCTAssertEqual(firstCount, 1)

        // The user re-authenticates: session B is persisted before A2's 401 lands.
        try await store.save(makeSession(token: "token-B"))
        await transport.releaseNext()
        _ = await a1.value
        _ = await a2.value

        let survivor = try await store.current()
        XCTAssertEqual(survivor?.token, "token-B", "a stale 401 must not delete the replacement session")
        let finalCount = await notifications.count
        XCTAssertEqual(finalCount, 1, "a stale 401 must not sign the new session out")
    }
}

actor NotificationCounter {
    private(set) var count = 0
    func increment() { count += 1 }
}

// MARK: 2. Persisted sessions are isolated by backend

final class SessionBackendIsolationTests: XCTestCase {
    private let prefix = "com.reclaimmytime.tests.backend.\(UUID().uuidString)"

    func testServiceNamesDifferByBackend() {
        let mock = SessionStore.service(for: .mock, prefix: prefix)
        let local = SessionStore.service(for: .live(URL(string: "http://localhost:8000")!), prefix: prefix)
        let prod = SessionStore.service(for: .live(URL(string: "https://api.reclaimmytime.app")!), prefix: prefix)
        XCTAssertEqual(Set([mock, local, prod]).count, 3)
        XCTAssertTrue(mock.hasPrefix(prefix))
    }

    func testMockSessionIsInvisibleToLiveAndSurvivesLiveSignOut() async throws {
        let mock = SessionStore.forBackend(.mock, prefix: prefix)
        let live = SessionStore.forBackend(.live(URL(string: "http://localhost:8000")!), prefix: prefix)
        defer { Task { await mock.clear(); await live.clear() } }

        try await mock.save(makeSession(token: "mock-token"))
        let liveCurrent = try await live.current()
        XCTAssertNil(liveCurrent, "switching to live must not reuse the mock credential")

        await live.clear()
        let mockReloaded = try await SessionStore.forBackend(.mock, prefix: prefix).current()
        XCTAssertEqual(mockReloaded?.token, "mock-token", "clearing live must not delete the mock credential")
    }

    func testLiveSessionIsInvisibleToMockAndSurvivesMockSignOut() async throws {
        let mock = SessionStore.forBackend(.mock, prefix: prefix)
        let live = SessionStore.forBackend(.live(URL(string: "http://localhost:8000")!), prefix: prefix)
        defer { Task { await mock.clear(); await live.clear() } }

        try await live.save(makeSession(token: "live-token"))
        let mockCurrent = try await mock.current()
        XCTAssertNil(mockCurrent, "switching to mock must not reuse the live credential")

        await mock.clear()
        let liveReloaded = try await SessionStore.forBackend(.live(URL(string: "http://localhost:8000")!), prefix: prefix).current()
        XCTAssertEqual(liveReloaded?.token, "live-token", "clearing mock must not delete the live credential")
    }
}

// MARK: 3. A Keychain read failure is not "no session"

/// In-memory keychain whose reads fail a set number of times first.
final class FlakyKeychain: KeychainStoring, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [String: Data] = [:]
    private var failuresRemaining: Int
    private(set) var readCount = 0

    init(failingReads: Int, items: [String: Data] = [:]) {
        self.failuresRemaining = failingReads
        self.items = items
    }

    func read(account: String) throws -> Data? {
        try lock.withLock {
            readCount += 1
            if failuresRemaining > 0 {
                failuresRemaining -= 1
                throw KeychainError.unexpectedStatus(errSecInteractionNotAllowed)
            }
            return items[account]
        }
    }

    func write(_ data: Data, account: String) throws {
        lock.withLock { items[account] = data }
    }

    func delete(account: String) throws {
        lock.withLock { items[account] = nil }
    }
}

@MainActor
final class KeychainReadFailureTests: XCTestCase {
    private func storedItems() throws -> [String: Data] {
        let session = StoredSession(try makeSession(token: "kept-token"))
        return [SessionStore.account: try JSONEncoder().encode(session)]
    }

    func testFailedReadThrowsAndStaysRetryable() async throws {
        let keychain = FlakyKeychain(failingReads: 1, items: try storedItems())
        let store = SessionStore(keychain: keychain)
        do {
            _ = try await store.current()
            XCTFail("expected a keychain error")
        } catch let error as SessionStoreError {
            XCTAssertEqual(error, .keychainUnavailable)
        }
        let retried = try await store.current()
        XCTAssertEqual(retried?.token, "kept-token", "the failed read was not cached as 'no session'")
        _ = try await store.current()
        XCTAssertEqual(keychain.readCount, 2, "a successful read is cached")
    }

    func testAbsentItemIsCachedAsNoSession() async throws {
        let keychain = FlakyKeychain(failingReads: 0)
        let store = SessionStore(keychain: keychain)
        let first = try await store.current()
        let second = try await store.current()
        XCTAssertNil(first)
        XCTAssertNil(second)
        XCTAssertEqual(keychain.readCount, 1)
    }

    func testBootstrapShowsRetryStateThenRecovers() async throws {
        let keychain = FlakyKeychain(failingReads: 1, items: try storedItems())
        let store = SessionStore(keychain: keychain)
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: mock, sessionStore: store)
        let auth = AuthState(api: api, sessionStore: store)

        await auth.bootstrap()
        XCTAssertEqual(auth.phase, .sessionUnavailable, "a read failure is not 'signed out'")

        await auth.bootstrap()
        guard case .signedIn(let session) = auth.phase else { return XCTFail("expected signed in after retry") }
        XCTAssertEqual(session.token, "kept-token")
    }
}

// MARK: 4. Sign-out is local-first

@MainActor
final class LocalFirstSignOutTests: XCTestCase {
    func testHangingRevokeStillSignsOutImmediately() async throws {
        let (store, _) = makeTestSessionStore()
        defer { Task { await store.clear() } }
        try await store.save(makeSession(token: "token-A"))
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: HangingTransport(), sessionStore: store)
        let auth = AuthState(api: api, sessionStore: store, revocationTimeout: .milliseconds(200))
        await auth.bootstrap()
        guard case .signedIn = auth.phase else { return XCTFail("expected signed in") }

        let signOut = Task { await auth.signOut() }
        let signedOut = await waitUntil(timeout: .seconds(1)) { auth.phase == .signedOut }
        signOut.cancel()
        XCTAssertTrue(signedOut, "sign-out must not wait for remote revocation")
        let stored = try await store.current()
        XCTAssertNil(stored)
    }
}

// MARK: 5. LocalDate validates real calendar dates

final class LocalDateCalendarTests: XCTestCase {
    func testRejectsImpossibleDates() {
        XCTAssertNil(LocalDate("2026-02-31"))
        XCTAssertNil(LocalDate("2025-02-29"), "2025 is not a leap year")
        XCTAssertNil(LocalDate("1900-02-29"), "century not divisible by 400")
        XCTAssertNil(LocalDate("2026-04-31"))
        XCTAssertNil(LocalDate("2026-11-00"))
    }

    func testAcceptsRealDatesAndKeepsWireString() throws {
        XCTAssertEqual(LocalDate("2024-02-29")?.rawValue, "2024-02-29")
        XCTAssertEqual(LocalDate("2000-02-29")?.rawValue, "2000-02-29")
        XCTAssertEqual(LocalDate("2026-12-31")?.rawValue, "2026-12-31")
        let decoded = try JSONDecoder().decode([LocalDate].self, from: Data(#"["2024-02-29"]"#.utf8))
        XCTAssertEqual(decoded.first?.rawValue, "2024-02-29")
        XCTAssertThrowsError(try JSONDecoder().decode([LocalDate].self, from: Data(#"["2025-02-29"]"#.utf8)))
    }
}
