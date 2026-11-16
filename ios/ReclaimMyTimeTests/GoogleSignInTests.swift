import AuthenticationServices
import XCTest
@testable import ReclaimMyTime

/// T6.0: Google sign-in replaces Sign in with Apple. `POST /auth/google/start {nonce_hash}` →
/// ASWebAuthenticationSession → `reclaimmytime://auth/google/callback` → client-side state check →
/// `POST /auth/google/complete {auth_code, state, nonce}` → session (+ `gcal_connected`).
enum GoogleSignInSupport {
    static let state = "eyJwIjoibG9naW4iLCJuIjoiYWJjIn0.U2lnbmVkTG9naW5TdGF0ZQ"
    static let authCode = "4/0AeanS0aGoogleSignInCode"
    static let nonce = "q0dOv3zQpX9m8YfJk2LhR7tWc4nBs6aEu1iGy5oT_-A"

    static func authorizationURL(state: String = state) -> URL {
        var components = URLComponents(string: "https://accounts.google.com/o/oauth2/v2/auth")!
        components.queryItems = [
            URLQueryItem(name: "client_id", value: "1234567890-abc.apps.googleusercontent.com"),
            URLQueryItem(name: "scope", value: "openid email https://www.googleapis.com/auth/calendar.freebusy https://www.googleapis.com/auth/calendar.app.created"),
            URLQueryItem(name: "state", value: state),
        ]
        return components.url!
    }

    static func startBody(state: String = state) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["authorization_url": authorizationURL(state: state).absoluteString])
    }

    /// The `/auth/apple` AuthSession fixture body plus `gcal_connected` (omitted when nil).
    static func completeObject(gcalConnected: Any?) throws -> [String: Any] {
        var object = try T53Bodies.fixtureBody("post_auth_apple__new_user.json")
        if let gcalConnected { object["gcal_connected"] = gcalConnected }
        return object
    }

    static func completeBody(gcalConnected: Any?) throws -> Data {
        try JSONSerialization.data(withJSONObject: completeObject(gcalConnected: gcalConnected))
    }

    static func callback(_ items: [String: String]) -> URL {
        var components = URLComponents(string: "reclaimmytime://auth/google/callback")!
        components.queryItems = items.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        return components.url!
    }

    static func authorized(authCode: String = authCode, state: String = state) -> URL {
        callback(["result": "authorized", "auth_code": authCode, "state": state])
    }

    static func error(_ code: String) -> URL {
        callback(["result": "error", "code": code])
    }

    static func errorBody(_ code: String) throws -> Data {
        try GcalSyncSupport.errorBody(code)
    }

    /// A signed-out AppModel on `transport`, with fakes for every system service.
    @MainActor
    static func model(transport: any HTTPTransport) -> (AppModel, SessionStore) {
        let (store, _) = makeTestSessionStore()
        let model = AppModel(config: AppConfig(backend: .mock), transport: transport, sessionStore: store, defaults: T54.freshDefaults(), notificationCenter: FakeNotificationCenter(), backgroundScheduler: FakeBackgroundScheduler())
        return (model, store)
    }
}

// MARK: - nonce

final class GoogleNonceTests: XCTestCase {
    func testSHA256Base64URLKnownVectors() {
        XCTAssertEqual(Nonce.sha256Base64URL("abc"), "ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0")
        // RFC 7636 appendix B (same transform as S256).
        XCTAssertEqual(Nonce.sha256Base64URL("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    }

    func testHashIsUnpaddedBase64URL() {
        let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        for _ in 0..<50 {
            let hash = Nonce.sha256Base64URL(Nonce.googleRaw())
            XCTAssertEqual(hash.count, 43, "32-byte digest, no padding")
            XCTAssertTrue(hash.allSatisfy(allowed.contains), hash)
        }
    }

    func testRawNonceLengthCharsetAndUniqueness() {
        let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        var seen = Set<String>()
        for _ in 0..<200 {
            let nonce = Nonce.googleRaw()
            XCTAssertEqual(nonce.count, 43, "32 random bytes, base64url without padding")
            XCTAssertTrue(nonce.allSatisfy(allowed.contains), nonce)
            seen.insert(nonce)
        }
        XCTAssertEqual(seen.count, 200)
        XCTAssertEqual(Nonce.googleRaw(byteCount: 48).count, 64)
    }

    func testBase64URLStripsPaddingAndSwapsAlphabet() {
        XCTAssertEqual(Nonce.base64URL(Data([0xfb, 0xff])), "-_8")
        XCTAssertEqual(Nonce.base64URL(Data()), "")
    }
}

// MARK: - models

@MainActor
final class GoogleSignInModelTests: XCTestCase {
    private let decoder = JSONCoding.makeDecoder()

    func testResultDecodesAuthSessionPlusGcalConnected() throws {
        let apple = try decoder.decode(AuthSession.self, from: XCTUnwrap(TestFixtures.named("post_auth_apple__new_user.json").responseBody))
        for flag in [true, false] {
            let result = try decoder.decode(GoogleSignInResult.self, from: GoogleSignInSupport.completeBody(gcalConnected: flag))
            XCTAssertEqual(result.auth, apple, "exactly the AuthSession schema")
            XCTAssertEqual(result.gcalConnected, flag)
        }
    }

    func testResultRequiresGcalConnected() throws {
        XCTAssertThrowsError(try decoder.decode(GoogleSignInResult.self, from: GoogleSignInSupport.completeBody(gcalConnected: nil))) { error in
            guard case DecodingError.keyNotFound(let key, _)? = error as? DecodingError else { return XCTFail("\(error)") }
            XCTAssertEqual(key.stringValue, "gcal_connected")
        }
        XCTAssertThrowsError(try decoder.decode(GoogleSignInResult.self, from: GoogleSignInSupport.completeBody(gcalConnected: NSNull())), "null is not a boolean")
        XCTAssertThrowsError(try decoder.decode(GoogleSignInResult.self, from: GoogleSignInSupport.completeBody(gcalConnected: "true")), "a string is not a boolean")
    }

    func testResultWithoutAuthSessionFieldsFails() throws {
        var object = try GoogleSignInSupport.completeObject(gcalConnected: true)
        object["token"] = nil
        XCTAssertThrowsError(try decoder.decode(GoogleSignInResult.self, from: JSONSerialization.data(withJSONObject: object)))
    }

    func testRequestBodiesMatchTheContract() throws {
        let encoder = JSONCoding.makeEncoder()
        assertJSONEqual(try encoder.encode(GoogleSignInStartRequest(nonceHash: "h")), Data(#"{"nonce_hash":"h"}"#.utf8), "start body")
        assertJSONEqual(try encoder.encode(GoogleSignInCompleteRequest(authCode: "c", state: "s", nonce: "n")),
                        Data(#"{"auth_code":"c","state":"s","nonce":"n"}"#.utf8), "complete body")
    }

    func testStartExposesTheAuthorizationURLState() throws {
        let start = try decoder.decode(GoogleSignInStart.self, from: GoogleSignInSupport.startBody())
        XCTAssertEqual(start.state, GoogleSignInSupport.state)
    }

    func testDescriptionsNeverContainSecrets() throws {
        let request = GoogleSignInCompleteRequest(authCode: GoogleSignInSupport.authCode, state: GoogleSignInSupport.state, nonce: GoogleSignInSupport.nonce)
        let result = try decoder.decode(GoogleSignInResult.self, from: GoogleSignInSupport.completeBody(gcalConnected: true))
        for text in [String(describing: request), String(reflecting: request), "\(request)"] {
            XCTAssertFalse(text.contains(GoogleSignInSupport.authCode), text)
            XCTAssertFalse(text.contains(GoogleSignInSupport.nonce), text)
            XCTAssertFalse(text.contains(GoogleSignInSupport.state), text)
        }
        for text in [String(describing: result), String(reflecting: result), "\(result)"] {
            XCTAssertFalse(text.contains(result.auth.token), text)
        }
    }

    func testCallbackRoutesDoNotCrossParse() {
        let signIn = GoogleSignInSupport.authorized()
        XCTAssertEqual(GcalCallbackResult(url: signIn, route: .signIn), .authorized(authCode: GoogleSignInSupport.authCode, state: GoogleSignInSupport.state))
        XCTAssertNil(GcalCallbackResult(url: signIn), "a sign-in callback is not a connect callback")
        let connect = CalendarConsentTests.authorized()
        XCTAssertNil(GcalCallbackResult(url: connect, route: .signIn), "a connect callback is not a sign-in callback")
        XCTAssertEqual(GcalCallbackResult(url: GoogleSignInSupport.callback(["result": "authorized", "auth_code": "c"]), route: .signIn),
                       .error(code: "gcal_state_invalid"), "authorized without state can't be completed")
        XCTAssertNil(GcalCallbackResult(url: GoogleSignInSupport.callback(["result": "error"]), route: .signIn))
    }
}

// MARK: - flow

@MainActor
final class GoogleSignInFlowTests: XCTestCase {
    private var gate: GatedTransport!

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
    }

    private func requests(_ path: String) async -> [URLRequest] {
        await gate.requests.filter { $0.httpMethod == "POST" && $0.url?.path() == path }
    }

    private func arm(gcalConnected: Bool) async throws {
        await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
        await gate.respond("POST", "/auth/google/complete", body: try GoogleSignInSupport.completeBody(gcalConnected: gcalConnected))
    }

    private func assertSignedOut(_ model: AppModel, _ store: SessionStore, file: StaticString = #filePath, line: UInt = #line) async throws {
        XCTAssertEqual(model.auth.phase, .signedOut, file: file, line: line)
        XCTAssertFalse(model.auth.isSigningIn, file: file, line: line)
        let stored = try await store.current()
        XCTAssertNil(stored, "nothing stored", file: file, line: line)
    }

    func testHappyPathWithCalendarSkipsTheConsentStep() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        model.handleAuthPhase(model.auth.phase)
        try await arm(gcalConnected: true)
        var opened: (URL, String)?
        await model.auth.signInWithGoogle(using: { url, scheme in
            opened = (url, scheme)
            return GoogleSignInSupport.authorized()
        }, nonce: GoogleSignInSupport.nonce)

        XCTAssertEqual(opened?.0, GoogleSignInSupport.authorizationURL(), "opens start's authorization_url")
        XCTAssertEqual(opened?.1, "reclaimmytime")

        let starts = await requests("/auth/google/start")
        let start = try XCTUnwrap(starts.first)
        assertJSONEqual(try XCTUnwrap(start.httpBody), Data(#"{"nonce_hash":"\#(Nonce.sha256Base64URL(GoogleSignInSupport.nonce))"}"#.utf8), "start body: hash only")
        XCTAssertNil(start.value(forHTTPHeaderField: "Authorization"), "unauthenticated")
        XCTAssertEqual(start.value(forHTTPHeaderField: "X-Timezone"), TimeZone.current.identifier)

        let completes = await requests("/auth/google/complete")
        XCTAssertEqual(completes.count, 1)
        let complete = try XCTUnwrap(completes.first)
        assertJSONEqual(try XCTUnwrap(complete.httpBody),
                        Data(#"{"auth_code":"\#(GoogleSignInSupport.authCode)","state":"\#(GoogleSignInSupport.state)","nonce":"\#(GoogleSignInSupport.nonce)"}"#.utf8),
                        "complete body: the RAW nonce")
        XCTAssertNil(complete.value(forHTTPHeaderField: "Authorization"), "unauthenticated")

        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("expected signed in") }
        let stored = try await store.current()
        XCTAssertEqual(stored, session, "stored like an Apple session")
        XCTAssertNil(model.auth.signInFailure)
        XCTAssertFalse(model.auth.isSigningIn)
        XCTAssertEqual(model.calendar.activeUserId, session.userId)
        XCTAssertEqual(model.calendar.status, .connected)
        XCTAssertFalse(model.calendar.needsConsentStep(userId: session.userId), "onboarding skips Connect Google Calendar")
    }

    func testHappyPathWithoutCalendarKeepsTheConsentStep() async throws {
        let (model, _) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        try await arm(gcalConnected: false)
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce)

        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("expected signed in") }
        XCTAssertEqual(model.calendar.activeUserId, session.userId)
        XCTAssertNotEqual(model.calendar.status, .connected)
        XCTAssertTrue(model.calendar.needsConsentStep(userId: session.userId), "the existing /gcal/connect step stays")
    }

    func testCalendarGrantIsInPlaceBeforeThePhaseFlips() async throws {
        let (model, _) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        try await arm(gcalConnected: true)
        let wired = try XCTUnwrap(model.auth.onGoogleSignIn)
        var phaseAtHook: AuthState.Phase?
        var consentNeededAfterHook: Bool?
        model.auth.onGoogleSignIn = { session, connected in
            wired(session, connected)
            // RootView routes on the phase change that follows; the grant must already be visible.
            phaseAtHook = model.auth.phase
            consentNeededAfterHook = model.calendar.needsConsentStep(userId: session.userId)
        }
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce)
        XCTAssertEqual(phaseAtHook, .signedOut, "the hook runs before the phase flips")
        XCTAssertEqual(consentNeededAfterHook, false)
        guard case .signedIn = model.auth.phase else { return XCTFail("expected signed in") }
    }

    func testStateMismatchIsRejectedWithoutCallingComplete() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        try await arm(gcalConnected: true)
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized(state: "someone-elses-state") }, nonce: GoogleSignInSupport.nonce)

        let completes = await requests("/auth/google/complete")
        XCTAssertEqual(completes.count, 0, "never forwarded to the server")
        XCTAssertEqual(model.auth.signInFailure, .googleFailed)
        try await assertSignedOut(model, store)
    }

    func testAuthorizationURLWithoutStateRejectsEveryCallback() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        await gate.respond("POST", "/auth/google/start", body: try JSONSerialization.data(withJSONObject: ["authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"]))
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce)
        let completes = await requests("/auth/google/complete")
        XCTAssertEqual(completes.count, 0)
        XCTAssertEqual(model.auth.signInFailure, .googleFailed)
        try await assertSignedOut(model, store)
    }

    func testCallbackWithoutStateIsRejected() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        try await arm(gcalConnected: true)
        await model.auth.signInWithGoogle(using: { _, _ in
            GoogleSignInSupport.callback(["result": "authorized", "auth_code": GoogleSignInSupport.authCode])
        }, nonce: GoogleSignInSupport.nonce)
        let completes = await requests("/auth/google/complete")
        XCTAssertEqual(completes.count, 0)
        XCTAssertEqual(model.auth.signInFailure, .googleFailed)
        try await assertSignedOut(model, store)
    }

    func testCallbackErrorCodes() async throws {
        let expected: [(String, AuthState.SignInFailure)] = [
            ("gcal_access_denied", .googleDenied),
            ("gcal_exchange_failed", .googleFailed),
            ("gcal_state_invalid", .googleFailed),
            ("something_new", .googleFailed),
        ]
        for (code, failure) in expected {
            let (model, store) = GoogleSignInSupport.model(transport: gate)
            await model.auth.bootstrap()
            await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
            await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.error(code) }, nonce: GoogleSignInSupport.nonce)
            XCTAssertEqual(model.auth.signInFailure, failure, code)
            try await assertSignedOut(model, store)
        }
        let completes = await requests("/auth/google/complete")
        XCTAssertEqual(completes.count, 0)
        XCTAssertEqual(AuthState.SignInFailure.googleDenied.message, "Google sign-in was cancelled. Try again when you're ready.")
        XCTAssertEqual(AuthState.SignInFailure.googleFailed.message, "Google sign-in didn't go through. Try again.")
    }

    func testUnrecognisedCallbackFails() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        try await arm(gcalConnected: true)
        // A connect-flow callback answering a sign-in.
        await model.auth.signInWithGoogle(using: { _, _ in CalendarConsentTests.authorized(state: GoogleSignInSupport.state) }, nonce: GoogleSignInSupport.nonce)
        let completes = await requests("/auth/google/complete")
        XCTAssertEqual(completes.count, 0)
        XCTAssertEqual(model.auth.signInFailure, .googleFailed)
        try await assertSignedOut(model, store)
    }

    func testUserCancelIsNotAFailure() async throws {
        let cancellations: [any Error] = [ASWebAuthenticationSessionError(.canceledLogin), CancellationError()]
        for cancellation in cancellations {
            let (model, store) = GoogleSignInSupport.model(transport: gate)
            await model.auth.bootstrap()
            await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
            await model.auth.signInWithGoogle(using: { _, _ in throw cancellation }, nonce: GoogleSignInSupport.nonce)
            XCTAssertNil(model.auth.signInFailure, "\(cancellation)")
            try await assertSignedOut(model, store)
        }
        let completes = await requests("/auth/google/complete")
        XCTAssertEqual(completes.count, 0)
    }

    func testCancelAfterAFailureClearsTheCallout() async throws {
        let (model, _) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.error("gcal_exchange_failed") }, nonce: GoogleSignInSupport.nonce)
        XCTAssertEqual(model.auth.signInFailure, .googleFailed)
        await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
        await model.auth.signInWithGoogle(using: { _, _ in throw ASWebAuthenticationSessionError(.canceledLogin) }, nonce: GoogleSignInSupport.nonce)
        XCTAssertNil(model.auth.signInFailure, "a new attempt clears the old callout")
    }

    func testCompleteErrorsSurfaceAndStoreNothing() async throws {
        for (status, code) in [(400, "gcal_state_invalid"), (502, "gcal_exchange_failed")] {
            let (model, store) = GoogleSignInSupport.model(transport: gate)
            await model.auth.bootstrap()
            await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
            await gate.respond("POST", "/auth/google/complete", status: status, body: try GoogleSignInSupport.errorBody(code))
            await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce)
            XCTAssertEqual(model.auth.signInFailure, .googleFailed, code)
            try await assertSignedOut(model, store)
            XCTAssertNil(model.calendar.activeUserId, "no account bound")
        }
    }

    func testStartFailureIsGeneric() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        await gate.respond("POST", "/auth/google/start", status: 500, body: try GoogleSignInSupport.errorBody(KnownErrorCode.internalError))
        var opened = false
        await model.auth.signInWithGoogle(using: { _, _ in
            opened = true
            return GoogleSignInSupport.authorized()
        }, nonce: GoogleSignInSupport.nonce)
        XCTAssertFalse(opened, "no browser without an authorization URL")
        XCTAssertEqual(model.auth.signInFailure, .other)
        try await assertSignedOut(model, store)
    }

    func testCompleteMissingGcalConnectedIsAFailure() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
        await gate.respond("POST", "/auth/google/complete", body: try GoogleSignInSupport.completeBody(gcalConnected: nil))
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce)
        XCTAssertEqual(model.auth.signInFailure, .other)
        try await assertSignedOut(model, store)
    }

    func testConcurrentTapIsIgnored() async throws {
        let (model, _) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        try await arm(gcalConnected: true)
        await gate.hold("POST", "/auth/google/start")
        let first = Task { await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce) }
        await waitUntil { await self.gate.waitingCount("POST", "/auth/google/start") == 1 }
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() })
        await gate.release("POST", "/auth/google/start")
        await first.value
        let starts = await requests("/auth/google/start")
        XCTAssertEqual(starts.count, 1)
        guard case .signedIn = model.auth.phase else { return XCTFail("expected signed in") }
    }

    func testEachAttemptUsesAFreshNonce() async throws {
        let (model, _) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        for _ in 0..<2 {
            await gate.respond("POST", "/auth/google/start", body: try GoogleSignInSupport.startBody())
            await model.auth.signInWithGoogle(using: { _, _ in throw ASWebAuthenticationSessionError(.canceledLogin) })
        }
        let hashes = try await requests("/auth/google/start").map {
            try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap($0.httpBody)) as? [String: String])["nonce_hash"]
        }
        XCTAssertEqual(hashes.count, 2)
        XCTAssertNotEqual(hashes[0], hashes[1])
    }

    func testColdStartSyncFiresOnceAfterGoogleSignIn() async throws {
        let (model, _) = GoogleSignInSupport.model(transport: gate)
        model.handleScenePhase(.active)  // before auth has a session
        await model.auth.bootstrap()
        model.handleAuthPhase(model.auth.phase)
        try await arm(gcalConnected: true)
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce)
        model.handleAuthPhase(model.auth.phase)
        model.handleAuthPhase(model.auth.phase)  // RootView's onChange may report it again
        await waitUntil { await self.requests("/gcal/sync").count == 1 }
        try await Task.sleep(for: .milliseconds(100))
        let syncs = await requests("/gcal/sync")
        XCTAssertEqual(syncs.count, 1, "one foreground pass for the new session")
        guard case .signedIn(let session) = model.auth.phase else { return XCTFail() }
        XCTAssertEqual(syncs.first?.value(forHTTPHeaderField: "Authorization"), "Bearer \(session.token)")
    }

    func testSignOutAfterGoogleSignInUnbindsTheCalendar() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        try await arm(gcalConnected: true)
        await model.auth.signInWithGoogle(using: { _, _ in GoogleSignInSupport.authorized() }, nonce: GoogleSignInSupport.nonce)
        model.handleAuthPhase(model.auth.phase)
        await model.auth.signOut()
        model.handleAuthPhase(model.auth.phase)
        XCTAssertNil(model.calendar.activeUserId)
        XCTAssertEqual(model.calendar.status, .unknown)
        try await assertSignedOut(model, store)
    }

    // Apple stays in the tree (unused) but can now answer 503 `apple_unavailable`.

    func testAppleUnavailableIsAnInlineFailure() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: gate)
        await model.auth.bootstrap()
        await gate.respond("POST", "/auth/apple", status: 503, body: try GoogleSignInSupport.errorBody(KnownErrorCode.appleUnavailable))
        await model.auth.signInWithApple(identityToken: "t", authorizationCode: "c", rawNonce: Nonce.random())
        XCTAssertEqual(model.auth.signInFailure, .appleRejected)
        try await assertSignedOut(model, store)
    }
}

// MARK: - mock mode

@MainActor
final class GoogleSignInMockModeTests: XCTestCase {
    private func mockAuthenticator(arguments: [String] = []) -> WebAuthenticator {
        { url, _ in MockWebAuthenticator.signInCallbackURL(authorizationURL: url, arguments: arguments) }
    }

    func testMockSignInIsWalkable() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        await model.auth.bootstrap()
        await model.auth.signInWithGoogle(using: mockAuthenticator())
        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("expected signed in: \(String(describing: model.auth.signInFailure))") }
        let stored = try await store.current()
        XCTAssertEqual(stored, session)
        XCTAssertFalse(model.calendar.needsConsentStep(userId: session.userId), "the mock grants the calendar")
        XCTAssertEqual(model.calendar.status, .connected)
    }

    func testBundledMockSignInIsWalkable() async throws {
        let (model, _) = GoogleSignInSupport.model(transport: MockTransport.bundled(frameDelay: .zero))
        await model.auth.bootstrap()
        await model.auth.signInWithGoogle(using: mockAuthenticator())
        guard case .signedIn = model.auth.phase else { return XCTFail("expected signed in: \(String(describing: model.auth.signInFailure))") }
    }

    func testMockErrorCallbackArgument() async throws {
        let (model, store) = GoogleSignInSupport.model(transport: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        await model.auth.bootstrap()
        await model.auth.signInWithGoogle(using: mockAuthenticator(arguments: ["app", "-MockGoogleSignInResult", "gcal_access_denied"]))
        XCTAssertEqual(model.auth.signInFailure, .googleDenied)
        let stored = try await store.current()
        XCTAssertNil(stored)
    }

    func testMockCallbackEchoesTheAuthorizationURLState() {
        let url = MockWebAuthenticator.signInCallbackURL(authorizationURL: GoogleSignInSupport.authorizationURL(), arguments: [])
        XCTAssertEqual(GcalCallbackResult(url: url, route: .signIn), .authorized(authCode: MockWebAuthenticator.fixtureAuthCode, state: GoogleSignInSupport.state))
        let connectArg = MockWebAuthenticator.signInCallbackURL(authorizationURL: GoogleSignInSupport.authorizationURL(), arguments: ["app", "-MockGcalResult", "gcal_access_denied"])
        XCTAssertEqual(GcalCallbackResult(url: connectArg, route: .signIn), .authorized(authCode: MockWebAuthenticator.fixtureAuthCode, state: GoogleSignInSupport.state), "-MockGcalResult is connect-only")
    }

    private func mockModel(_ configure: (MockTransport) async -> Void = { _ in }) async throws -> (AppModel, SessionStore) {
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        await configure(mock)
        let (model, store) = GoogleSignInSupport.model(transport: mock)
        await model.auth.bootstrap()
        return (model, store)
    }

    func testFixtureWithoutCalendarKeepsTheConsentStep() async throws {
        let (model, _) = try await mockModel { await $0.setVariant("without_calendar", for: "completeGoogleSignIn") }
        await model.auth.signInWithGoogle(using: mockAuthenticator())
        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("expected signed in") }
        XCTAssertTrue(model.calendar.needsConsentStep(userId: session.userId))
        XCTAssertNotEqual(model.calendar.status, .connected)
    }

    func testFixtureCompleteErrors() async throws {
        for variant in ["exchange_failed", "state_invalid"] {
            let (model, store) = try await mockModel { await $0.setVariant(variant, for: "completeGoogleSignIn") }
            await model.auth.signInWithGoogle(using: mockAuthenticator())
            XCTAssertEqual(model.auth.signInFailure, .googleFailed, variant)
            let stored = try await store.current()
            XCTAssertNil(stored, variant)
        }
    }

    func testFixtureAppleUnconfiguredIsAnInlineFailure() async throws {
        let (model, store) = try await mockModel { await $0.setVariant("unconfigured", for: "signInWithApple") }
        await model.auth.signInWithApple(identityToken: "t", authorizationCode: "c", rawNonce: Nonce.random())
        XCTAssertEqual(model.auth.signInFailure, .appleRejected)
        XCTAssertEqual(model.auth.phase, .signedOut)
        let stored = try await store.current()
        XCTAssertNil(stored)
    }

    func testFallbacksStandInOnlyWhenNoFixtureServesThePath() async throws {
        let fixtures = try TestFixtures.all()
        XCTAssertTrue(MockTransport.googleSignInFallbacks(for: fixtures).isEmpty, "contract fixtures serve both routes")

        // A fixture set without the Google routes (e.g. a stale bundle) still walks sign-in.
        let trimmed = fixtures.filter { !$0.path.hasPrefix("/auth/google/") }
        let synthesized = MockTransport.googleSignInFallbacks(for: trimmed)
        XCTAssertEqual(Set(synthesized.map(\.path)), [MockTransport.googleSignInStartPath, MockTransport.googleSignInCompletePath])
        XCTAssertEqual(Set(synthesized.map(\.operation)), ["startGoogleSignIn", "completeGoogleSignIn"])
        let decoder = JSONCoding.makeDecoder()
        for fixture in synthesized {
            let body = try XCTUnwrap(fixture.responseBody)
            if fixture.path == MockTransport.googleSignInStartPath {
                XCTAssertNotNil(try decoder.decode(GoogleSignInStart.self, from: body).state)
            } else {
                XCTAssertTrue(try decoder.decode(GoogleSignInResult.self, from: body).gcalConnected)
            }
        }
        let (model, _) = GoogleSignInSupport.model(transport: MockTransport(fixtures: trimmed, frameDelay: .zero))
        await model.auth.bootstrap()
        await model.auth.signInWithGoogle(using: mockAuthenticator())
        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("expected signed in: \(String(describing: model.auth.signInFailure))") }
        XCTAssertFalse(model.calendar.needsConsentStep(userId: session.userId))
    }
}

// MARK: - URL scheme stays unregistered

final class URLSchemeRegistrationTests: XCTestCase {
    private func schemes(in info: [String: Any]?) -> [String] {
        let types = info?["CFBundleURLTypes"] as? [[String: Any]] ?? []
        return types.flatMap { $0["CFBundleURLSchemes"] as? [String] ?? [] }.map { $0.lowercased() }
    }

    /// The callback must only reach the app's own ASWebAuthenticationSession (see
    /// `GcalWebAuthenticator`): registering the scheme would let any page deliver an auth code.
    func testReclaimMyTimeSchemeIsNotRegistered() throws {
        let appBundle = Bundle(for: AppModel.self)
        XCTAssertNotNil(appBundle.infoDictionary?["CFBundleIdentifier"])
        XCTAssertFalse(schemes(in: appBundle.infoDictionary).contains(GcalCallbackResult.scheme), "built Info.plist")
        XCTAssertFalse(schemes(in: Bundle.main.infoDictionary).contains(GcalCallbackResult.scheme), "host app Info.plist")

        let source = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "Config/Info.plist")
        let data = try Data(contentsOf: source)
        let plist = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
        XCTAssertFalse(schemes(in: plist).contains(GcalCallbackResult.scheme), "Config/Info.plist")
    }
}
