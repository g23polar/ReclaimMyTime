import XCTest
@testable import ReclaimMyTime

final class NonceTests: XCTestCase {
    func testSHA256HexKnownVectors() {
        XCTAssertEqual(Nonce.sha256Hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        XCTAssertEqual(Nonce.sha256Hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    }

    func testFixtureNonceHashIsLowercaseHex() {
        let digest = Nonce.sha256Hex("8tYq2Lw0VbN5xZr7Kc3Mf1Hd")
        XCTAssertEqual(digest.count, 64)
        XCTAssertTrue(digest.allSatisfy { "0123456789abcdef".contains($0) })
    }

    func testRandomNonceLengthCharsetAndUniqueness() {
        let allowed = Set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-._")
        var seen = Set<String>()
        for _ in 0..<200 {
            let nonce = Nonce.random()
            XCTAssertEqual(nonce.count, 32)
            XCTAssertGreaterThanOrEqual(nonce.count, 16, "contract minLength")
            XCTAssertTrue(nonce.allSatisfy(allowed.contains))
            seen.insert(nonce)
        }
        XCTAssertEqual(seen.count, 200)
        XCTAssertEqual(Nonce.random(length: 48).count, 48)
    }
}

final class KeychainStoreTests: XCTestCase {
    private let store = KeychainStore(service: "com.reclaimmytime.tests.keychain.\(UUID().uuidString)")

    override func tearDown() {
        try? store.delete(account: "a")
    }

    func testWriteReadOverwriteDelete() throws {
        XCTAssertNil(try store.read(account: "a"))
        try store.write(Data("one".utf8), account: "a")
        XCTAssertEqual(try store.read(account: "a"), Data("one".utf8))
        try store.write(Data("two".utf8), account: "a")
        XCTAssertEqual(try store.read(account: "a"), Data("two".utf8))
        try store.delete(account: "a")
        XCTAssertNil(try store.read(account: "a"))
        XCTAssertNoThrow(try store.delete(account: "a"), "deleting a missing item is not an error")
    }

    func testItemsAreAccessibleAfterFirstUnlock() throws {
        try store.write(Data("x".utf8), account: "a")
        XCTAssertEqual(try store.accessibility(account: "a"), kSecAttrAccessibleAfterFirstUnlock as String)
    }

    func testServicesAreIsolated() throws {
        let other = KeychainStore(service: store.service + ".other")
        try store.write(Data("x".utf8), account: "a")
        XCTAssertNil(try other.read(account: "a"))
    }
}

final class SessionStoreTests: XCTestCase {
    func testSaveLoadAcrossInstancesAndClear() async throws {
        let (store, keychain) = makeTestSessionStore()
        let auth = try TestFixtures.authSession()
        let saved = try await store.save(auth)
        XCTAssertEqual(saved.token, auth.token)
        XCTAssertEqual(saved.userId, auth.user.id)

        let reloaded = SessionStore(keychain: keychain)
        let current = try await reloaded.current()
        XCTAssertEqual(current, saved)
        let token = await reloaded.token()
        XCTAssertEqual(token, auth.token)

        await reloaded.clear()
        let afterClear = try await SessionStore(keychain: keychain).current()
        XCTAssertNil(afterClear)
    }

    func testDescriptionsNeverContainToken() throws {
        let auth = try TestFixtures.authSession()
        let stored = StoredSession(auth)
        for text in [String(describing: stored), String(reflecting: stored), "\(stored)", String(describing: auth), String(reflecting: auth)] {
            XCTAssertFalse(text.contains(auth.token), text)
        }
    }
}

@MainActor
final class AuthStateTests: XCTestCase {
    private func makeAuth() throws -> (AuthState, MockTransport, SessionStore) {
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        let (store, _) = makeTestSessionStore()
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: mock, sessionStore: store)
        return (AuthState(api: api, sessionStore: store), mock, store)
    }

    func testBootstrapSignInSignOut() async throws {
        let (auth, _, store) = try makeAuth()
        await auth.bootstrap()
        XCTAssertEqual(auth.phase, .signedOut)

        await auth.signInWithApple(identityToken: "t", authorizationCode: "c", rawNonce: Nonce.random())
        guard case .signedIn(let session) = auth.phase else { return XCTFail("expected signed in") }
        let stored = try await store.current()
        XCTAssertEqual(stored, session)
        XCTAssertNil(auth.signInFailure)

        await auth.signOut()
        XCTAssertEqual(auth.phase, .signedOut)
        let cleared = try await store.current()
        XCTAssertNil(cleared)
    }

    func testAppleRejectionSurfacesInlineFailure() async throws {
        let (auth, mock, store) = try makeAuth()
        await mock.setVariant("invalid_token", for: "signInWithApple")
        await auth.bootstrap()
        await auth.signInWithApple(identityToken: "t", authorizationCode: "c", rawNonce: Nonce.random())
        XCTAssertEqual(auth.phase, .signedOut)
        XCTAssertEqual(auth.signInFailure, .appleRejected)
        let stored = try await store.current()
        XCTAssertNil(stored)
    }

    func testCancelledAppleSheetIsNotAFailure() throws {
        let (auth, _, _) = try makeAuth()
        auth.appleAuthorizationFailed(cancelled: true)
        XCTAssertNil(auth.signInFailure)
        auth.appleAuthorizationFailed(cancelled: false)
        XCTAssertEqual(auth.signInFailure, .appleRejected)
    }

    func testUnauthenticatedResponseSignsOutViaRelay() async throws {
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        let (store, _) = makeTestSessionStore()
        try await store.save(TestFixtures.authSession())
        let relay = SessionInvalidationRelay()
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: mock, sessionStore: store,
                                      onUnauthenticated: { token in await relay.notify(token: token) })
        let auth = AuthState(api: api, sessionStore: store)
        relay.auth = auth
        await auth.bootstrap()
        guard case .signedIn = auth.phase else { return XCTFail("expected signed in") }

        await mock.setVariant("unauthenticated", for: "getMe")
        _ = try? await api.getMe()
        XCTAssertEqual(auth.phase, .signedOut)
    }
}

final class AppConfigTests: XCTestCase {
    private func defaults() -> UserDefaults {
        let name = "AppConfigTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name) ?? .standard
        defaults.removePersistentDomain(forName: name)
        return defaults
    }

    func testMockFlag() {
        XCTAssertTrue(AppConfig.fromProcess(arguments: ["app", "-UseMockAPI"], defaults: defaults()).usesMock)
        XCTAssertFalse(AppConfig.fromProcess(arguments: ["app"], defaults: defaults()).usesMock)
        let explicitNo = defaults()
        explicitNo.set(false, forKey: "UseMockAPI")
        XCTAssertFalse(AppConfig.fromProcess(arguments: ["app", "-UseMockAPI", "NO"], defaults: explicitNo).usesMock)
        let explicitYes = defaults()
        explicitYes.set(true, forKey: "UseMockAPI")
        XCTAssertTrue(AppConfig.fromProcess(arguments: ["app"], defaults: explicitYes).usesMock)
    }

    func testLiveBaseURL() {
        let custom = defaults()
        custom.set("https://api.example.test", forKey: "APIBaseURL")
        XCTAssertEqual(AppConfig.fromProcess(arguments: [], defaults: custom).backend, .live(URL(string: "https://api.example.test")!))
        XCTAssertEqual(AppConfig.fromProcess(arguments: [], defaults: defaults()).backend, .live(URL(string: AppConfig.defaultLiveBaseURL)!))
    }

    func testDefaultLiveBackendIsProd() {
        XCTAssertEqual(AppConfig.defaultLiveBaseURL, "https://reclaimmytime.fly.dev")
        let config = AppConfig.fromProcess(arguments: ["app"], defaults: defaults())
        XCTAssertFalse(config.usesMock)
        XCTAssertEqual(config.baseURL, URL(string: "https://reclaimmytime.fly.dev")!)
        XCTAssertEqual(config.baseURL.scheme, "https")
    }

    func testAPIBaseURLOverridesProd() {
        let custom = defaults()
        custom.set("http://localhost:8000", forKey: "APIBaseURL")
        XCTAssertEqual(AppConfig.fromProcess(arguments: ["app"], defaults: custom).baseURL, URL(string: "http://localhost:8000")!)
    }

    func testMockFlagWinsOverAPIBaseURL() {
        let custom = defaults()
        custom.set("https://api.example.test", forKey: "APIBaseURL")
        let config = AppConfig.fromProcess(arguments: ["app", "-UseMockAPI", "YES"], defaults: custom)
        XCTAssertEqual(config.backend, .mock)
        XCTAssertEqual(config.baseURL, URL(string: AppConfig.mockBaseURL)!)
    }

    func testProdSessionHasItsOwnKeychainService() {
        let prod = SessionStore.service(for: .live(URL(string: AppConfig.defaultLiveBaseURL)!), prefix: "p")
        XCTAssertEqual(prod, "p.live.reclaimmytime.fly.dev")
        XCTAssertNotEqual(prod, SessionStore.service(for: .mock, prefix: "p"))
        XCTAssertNotEqual(prod, SessionStore.service(for: .live(URL(string: "http://localhost:8000")!), prefix: "p"))
    }
}
