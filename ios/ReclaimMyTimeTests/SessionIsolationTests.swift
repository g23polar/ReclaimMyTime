import os
import XCTest
@testable import ReclaimMyTime

/// Holds requests whose path matches `gatedPath` until released, then serves them from the mock.
actor GatedMockTransport: HTTPTransport {
    let inner: MockTransport
    let gatedPath: String
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(inner: MockTransport, gatedPath: String) {
        self.inner = inner
        self.gatedPath = gatedPath
    }

    var pendingCount: Int { waiters.count }

    func releaseAll() {
        let all = waiters
        waiters.removeAll()
        all.forEach { $0.resume() }
    }

    private func gate(_ request: URLRequest) async {
        guard request.url?.path() == gatedPath else { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        await gate(request)
        return try await inner.data(for: request)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        await gate(request)
        return try await inner.stream(for: request)
    }
}

/// Never answers and ignores cancellation.
struct UncooperativeTransport: HTTPTransport {
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        await withCheckedContinuation { (_: CheckedContinuation<Void, Never>) in }
        throw URLError(.timedOut)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        await withCheckedContinuation { (_: CheckedContinuation<Void, Never>) in }
        throw URLError(.timedOut)
    }
}

@MainActor
final class SignOutIsolationTests: XCTestCase {
    private func harness(gating path: String) throws -> (AppModel, GatedMockTransport) {
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        let gated = GatedMockTransport(inner: mock, gatedPath: path)
        let suite = "Isolation.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        let (store, _) = makeTestSessionStore()
        return (AppModel(config: AppConfig(backend: .mock), transport: gated, sessionStore: store, defaults: defaults), gated)
    }

    private func waitForGate(_ gated: GatedMockTransport) async throws {
        for _ in 0..<400 where await gated.pendingCount == 0 {
            try await Task.sleep(for: .milliseconds(5))
        }
        let count = await gated.pendingCount
        XCTAssertEqual(count, 1, "request should be held at the gate")
    }

    func testConfirmCompletingAfterSignOutPublishesNothing() async throws {
        let changeset = try T52Fixtures.changeset("post_projects__perennial_onboarding.json")
        let (model, gated) = try harness(gating: "/changesets/\(changeset.id.pathComponent)/confirm")
        model.chat.debugAttach(changeset: changeset, text: "Reading.")
        let entryId = try XCTUnwrap(model.chat.entries.last?.id)

        let confirm = Task { await model.chat.confirmCards(entryId: entryId) }
        try await waitForGate(gated)
        model.resetSessionState()
        await gated.releaseAll()
        await confirm.value

        XCTAssertNil(model.pending.pending)
        XCTAssertEqual(model.pending.liveStateRevision, 0, "no outcome published for the old session")
        XCTAssertNil(model.chat.preferences, "onConfirmed must not run after sign-out")
        XCTAssertEqual(model.chat.entries.count, 1, "chat stays at the opening message")
        XCTAssertNil(model.projects.card(for: UUID(uuidString: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b03")!))
    }

    func testPreferencesSubmitCompletingAfterSignOutStagesNothing() async throws {
        let (model, gated) = try harness(gating: "/preferences")
        model.chat.showPreferencesPrompt()
        model.chat.preferences?.diversity = .mixed

        let submit = Task { await model.chat.submitPreferences() }
        try await waitForGate(gated)
        model.resetSessionState()
        model.navigation.selectedTab = .chat
        await gated.releaseAll()
        _ = await submit.value

        XCTAssertNil(model.pending.pending, "the previous account's changeset must not be staged")
        XCTAssertEqual(model.navigation.selectedTab, .chat, "no tab switch after sign-out")
    }

    func testAgentTurnCompletingAfterSignOutWritesNothing() async throws {
        let (model, gated) = try harness(gating: "/agent/message")
        let turn = Task { await model.chat.submit("I don't have my laptop today or tomorrow.") }
        try await waitForGate(gated)
        model.resetSessionState()
        await gated.releaseAll()
        await turn.value

        XCTAssertTrue(model.chat.transcript.isEmpty)
        XCTAssertEqual(model.chat.entries.count, 1)
        XCTAssertNil(model.pending.pending)
        XCTAssertFalse(model.chat.isStreaming)
    }
}

@MainActor
final class InvalidationIdentityTests: XCTestCase {
    private func signedInAuth(token: String) async throws -> AuthState {
        let (store, _) = makeTestSessionStore()
        try await store.save(AuthSession(token: token, session: TestFixtures.authSession().session, user: TestFixtures.authSession().user, isNewUser: false))
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: mock, sessionStore: store)
        let auth = AuthState(api: api, sessionStore: store)
        await auth.bootstrap()
        return auth
    }

    func testLateInvalidationForOldSessionIsIgnored() async throws {
        let auth = try await signedInAuth(token: "token-B")
        auth.sessionInvalidated(token: "token-A")
        guard case .signedIn(let session) = auth.phase else { return XCTFail("replacement session was signed out") }
        XCTAssertEqual(session.token, "token-B")
    }

    func testInvalidationForCurrentSessionSignsOut() async throws {
        let auth = try await signedInAuth(token: "token-B")
        auth.sessionInvalidated(token: "token-B")
        XCTAssertEqual(auth.phase, .signedOut)
    }

    func testRelayDeliversRejectedTokenAfterReplacementIsHarmless() async throws {
        let (store, _) = makeTestSessionStore()
        defer { Task { await store.clear() } }
        let fixture = try TestFixtures.authSession()
        try await store.save(AuthSession(token: "token-A", session: fixture.session, user: fixture.user, isNewUser: false))
        let relay = SessionInvalidationRelay()
        let gate = OSAllocatedUnfairLock<CheckedContinuation<Void, Never>?>(initialState: nil)
        let transport = GatedUnauthorizedTransport()
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: transport, sessionStore: store,
                                      onUnauthenticated: { token in
                                          // Delay delivery until the replacement session is in place.
                                          await withCheckedContinuation { cont in gate.withLock { $0 = cont } }
                                          await relay.notify(token: token)
                                      })
        let auth = AuthState(api: api, sessionStore: store)
        relay.auth = auth
        await auth.bootstrap()

        let request = Task { try? await api.getMe() }
        for _ in 0..<200 where await transport.pendingCount == 0 { try await Task.sleep(for: .milliseconds(5)) }
        await transport.releaseNext()
        for _ in 0..<200 where gate.withLock({ $0 == nil }) { try await Task.sleep(for: .milliseconds(5)) }

        // Session B signs in while A's invalidation is still in flight.
        try await store.save(AuthSession(token: "token-B", session: fixture.session, user: fixture.user, isNewUser: false))
        await auth.bootstrap()
        gate.withLock { $0?.resume(); $0 = nil }
        _ = await request.value

        guard case .signedIn(let session) = auth.phase else { return XCTFail("late notification signed out session B") }
        XCTAssertEqual(session.token, "token-B")
        let stored = try await store.current()
        XCTAssertEqual(stored?.token, "token-B")
    }
}

@MainActor
final class RevocationBoundTests: XCTestCase {
    func testRevocationTaskEndsAtTimeoutEvenIfRevokeIgnoresCancellation() async throws {
        let (store, _) = makeTestSessionStore()
        defer { Task { await store.clear() } }
        let fixture = try TestFixtures.authSession()
        try await store.save(AuthSession(token: "token-A", session: fixture.session, user: fixture.user, isNewUser: false))
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: UncooperativeTransport(), sessionStore: store)
        let auth = AuthState(api: api, sessionStore: store, revocationTimeout: .milliseconds(100))
        await auth.bootstrap()
        await auth.signOut()
        XCTAssertEqual(auth.phase, .signedOut)

        let revocation = try XCTUnwrap(auth.pendingRevocation)
        let finished = OSAllocatedUnfairLock(initialState: false)
        Task.detached { await revocation.value; finished.withLock { $0 = true } }
        var ended = false
        for _ in 0..<200 {
            if finished.withLock({ $0 }) { ended = true; break }
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertTrue(ended, "the revocation bound must not depend on the revoke honouring cancellation")
    }
}
