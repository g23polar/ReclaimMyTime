import XCTest
@testable import ReclaimMyTime

@MainActor
final class PreferencesStepTests: XCTestCase {
    func testSubmitStagesProposalShowsBannerAndOpensWeek() async throws {
        let h = try T52Harness()
        h.model.navigation.selectedTab = .chat
        h.chat.showPreferencesPrompt()
        let form = try XCTUnwrap(h.chat.preferences)
        XCTAssertFalse(form.canSubmit, "needs a focused/mixed choice")
        form.diversity = .mixed
        XCTAssertTrue(form.canSubmit)

        let changeset = await h.chat.submitPreferences()

        XCTAssertEqual(changeset?.id, try T52Fixtures.changeset("put_preferences__first_week.json").id)
        XCTAssertEqual(h.pending.pending?.id, changeset?.id, "banner visible: the store holds the pending changeset")
        XCTAssertEqual(ProposalSummary.make(try XCTUnwrap(h.pending.pending)).summary, "9 added")
        XCTAssertEqual(h.model.navigation.selectedTab, .week, "a changeset with a plan switches to Week")
        XCTAssertEqual(form.phase, .submitted)

        let puts = await h.transport.requests(method: "PUT", path: "/preferences")
        let put = try XCTUnwrap(puts.first?.httpBody)
        let sent = try JSONCoding.makeDecoder().decode(UserPreferences.self, from: put)
        XCTAssertEqual(sent.dayDiversity, .mixed)
        XCTAssertEqual(sent.schedulableWindows[.mon].map(\.start.rawValue), ["08:00"])
        XCTAssertEqual(sent.schedulableWindows[.fri].map(\.end.rawValue), ["18:00"])
        XCTAssertEqual(sent.schedulableWindows[.sun].map(\.start.rawValue), ["10:00"])
        XCTAssertEqual(sent.bufferMinutes, PreferencesFormModel.defaultBufferMinutes)
        XCTAssertEqual(sent.timezone, TimeZone.current.identifier)
        XCTAssertTrue((1...7).contains(sent.weekStart))
    }

    func testRejectedPreferencesProposalReopensForm() async throws {
        let h = try T52Harness()
        h.chat.showPreferencesPrompt()
        h.chat.preferences?.diversity = .focused
        let changeset = try await XCTUnwrapAsync(await h.chat.submitPreferences())
        await h.pending.reject(id: changeset.id)
        XCTAssertEqual(h.chat.preferences?.phase, .editing)
    }

    func testWindowValidation() {
        let overlap = PreferencesFormModel(rows: [
            .init(days: .weekdays, startMinutes: 8 * 60, endMinutes: 12 * 60),
            .init(days: .mon, startMinutes: 11 * 60, endMinutes: 13 * 60),
        ], diversity: .mixed)
        XCTAssertEqual(overlap.validationIssue, .overlap(.mon))

        let backwards = PreferencesFormModel(rows: [.init(days: .everyDay, startMinutes: 18 * 60, endMinutes: 9 * 60)], diversity: .mixed)
        XCTAssertEqual(backwards.validationIssue, .endBeforeStart)

        let adjacent = PreferencesFormModel(rows: [
            .init(days: .weekdays, startMinutes: 8 * 60, endMinutes: 12 * 60),
            .init(days: .weekdays, startMinutes: 12 * 60, endMinutes: 0),
        ], diversity: .focused)
        XCTAssertNil(adjacent.validationIssue, "touching windows are fine; 00:00 as an end means midnight")
        guard case .success(let windows) = adjacent.buildWindows() else { return XCTFail() }
        XCTAssertEqual(windows[.tue].map { "\($0.start)-\($0.end)" }, ["08:00-12:00", "12:00-24:00"])
        XCTAssertTrue(windows[.sat].isEmpty)

        let form = PreferencesFormModel()
        form.addRow()
        XCTAssertEqual(form.rows.count, 3)
        form.removeRow(form.rows[0].id)
        XCTAssertEqual(form.rows.count, 2)
    }

    func testIsoWeekStart() {
        var sundayFirst = Calendar(identifier: .gregorian)
        sundayFirst.firstWeekday = 1
        var mondayFirst = Calendar(identifier: .gregorian)
        mondayFirst.firstWeekday = 2
        var saturdayFirst = Calendar(identifier: .gregorian)
        saturdayFirst.firstWeekday = 7
        XCTAssertEqual(PreferencesFormModel.isoWeekStart(calendar: sundayFirst), 7)
        XCTAssertEqual(PreferencesFormModel.isoWeekStart(calendar: mondayFirst), 1)
        XCTAssertEqual(PreferencesFormModel.isoWeekStart(calendar: saturdayFirst), 6)
    }
}

@MainActor
final class CalendarConsentTests: XCTestCase {
    func testCallbackCodesMapToSpecCallouts() {
        XCTAssertEqual(CalendarConnectionModel.callout(forCode: "gcal_access_denied").message, "No problem. You can connect later in Settings.")
        XCTAssertEqual(CalendarConnectionModel.callout(forCode: "gcal_scope_insufficient").message, "Google didn't grant calendar access. Please try again and allow both permissions.")
        for code in ["gcal_state_invalid", "gcal_exchange_failed", "something_new"] {
            XCTAssertEqual(CalendarConnectionModel.callout(forCode: code).message, "Something went wrong with Google. Try again.")
        }
    }

    /// `state` of the `get_gcal_connect__ok` fixture's authorization_url.
    static let fixtureState = "eyJ1IjoiMGI2ZTJmMWEiLCJuIjoiN2YzYSJ9.Qm9ndXNTaWduYXR1cmVGb3JGaXh0dXJlcw"

    static func authorized(authCode: String = "4/0AeanS0aExampleAuthCode", state: String = fixtureState) -> URL {
        var components = URLComponents(string: "reclaimmytime://gcal/callback")!
        components.queryItems = [
            URLQueryItem(name: "result", value: "authorized"),
            URLQueryItem(name: "auth_code", value: authCode),
            URLQueryItem(name: "state", value: state),
        ]
        return components.url!
    }

    func testHappyPathPostsExactBodyThenConnects() async throws {
        let h = try T52Harness()
        let calendar = h.model.calendar
        var opened: (URL, String)?
        let connected = await calendar.connect { url, scheme in
            opened = (url, scheme)
            return Self.authorized()
        }
        XCTAssertTrue(connected)
        XCTAssertEqual(opened?.0.host(), "accounts.google.com", "opens GET /gcal/connect's authorization_url")
        XCTAssertEqual(opened?.1, "reclaimmytime")

        let posts = await h.transport.requests(method: "POST", path: "/gcal/complete")
        XCTAssertEqual(posts.count, 1)
        let post = try XCTUnwrap(posts.first)
        assertJSONEqual(try XCTUnwrap(post.httpBody),
                        Data(#"{"auth_code":"4/0AeanS0aExampleAuthCode","state":"\#(Self.fixtureState)"}"#.utf8),
                        "POST /gcal/complete body")
        XCTAssertEqual(post.value(forHTTPHeaderField: "X-Timezone"), TimeZone.current.identifier)

        XCTAssertEqual(calendar.status, .connected)
        XCTAssertEqual(calendar.connectedEmail, "reclaim.tester@gmail.com", "email comes from the returned GcalStatus")
        XCTAssertNil(calendar.callout)
    }

    func testStateMismatchNeverCallsServer() async throws {
        let h = try T52Harness()
        let calendar = h.model.calendar
        for callback in [
            Self.authorized(state: "attacker-state"),
            URL(string: "reclaimmytime://gcal/callback?result=authorized&auth_code=4%2F0Aean")!, // no state
            URL(string: "reclaimmytime://gcal/callback?result=authorized&state=\(Self.fixtureState)")!, // no code
        ] {
            let connected = await calendar.connect { _, _ in callback }
            XCTAssertFalse(connected, callback.absoluteString)
            XCTAssertEqual(calendar.callout, CalendarConnectionModel.callout(forCode: "gcal_state_invalid"), callback.absoluteString)
            XCTAssertEqual(calendar.callout, .failed)
            XCTAssertNotEqual(calendar.status, .connected)
        }
        let posts = await h.transport.requests(method: "POST", path: "/gcal/complete")
        XCTAssertTrue(posts.isEmpty, "a mismatched or incomplete callback must not reach the server")
    }

    func testStateCheckFailsWhenAuthorizationURLHasNoState() async throws {
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        let stub = GcalStubTransport(inner: mock)
        await stub.overrideConnect(authorizationURL: "https://accounts.google.com/o/oauth2/v2/auth?client_id=x")
        let calendar = try Self.calendarModel(transport: stub)
        let connected = await calendar.connect { _, _ in Self.authorized() }
        XCTAssertFalse(connected)
        XCTAssertEqual(calendar.callout, .failed)
        let posts = await stub.requests(method: "POST", path: "/gcal/complete")
        XCTAssertTrue(posts.isEmpty)
    }

    func testCompleteErrorsMapToCallouts() async throws {
        let cases: [(status: Int, code: String, callout: CalendarConnectionModel.Callout)] = [
            (400, "gcal_state_invalid", .failed),
            (422, "gcal_scope_insufficient", .scopeInsufficient),
            (502, "gcal_exchange_failed", .failed),
        ]
        for (status, code, callout) in cases {
            let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
            let stub = GcalStubTransport(inner: mock)
            await stub.failComplete(status: status, code: code)
            let calendar = try Self.calendarModel(transport: stub)
            calendar.activate(userId: UUID())
            let connected = await calendar.connect { _, _ in Self.authorized() }
            XCTAssertFalse(connected, code)
            XCTAssertEqual(calendar.callout, callout, code)
            XCTAssertEqual(calendar.callout, CalendarConnectionModel.callout(forCode: code), "same code, same callout as the redirect")
            XCTAssertNotEqual(calendar.status, .connected, code)
            XCTAssertFalse(calendar.isConnecting)
            let posts = await stub.requests(method: "POST", path: "/gcal/complete")
            XCTAssertEqual(posts.count, 1, code)
        }
    }

    func testDeniedAndCancelledOutcomes() async throws {
        let h = try T52Harness()
        let calendar = h.model.calendar
        let denied = await calendar.connect { _, _ in URL(string: "reclaimmytime://gcal/callback?result=error&code=gcal_access_denied")! }
        XCTAssertFalse(denied)
        XCTAssertEqual(calendar.callout, .accessDenied)
        XCTAssertEqual(calendar.status, .notConnected)

        let exchange = await calendar.connect { _, _ in URL(string: "reclaimmytime://gcal/callback?result=error&code=gcal_exchange_failed")! }
        XCTAssertFalse(exchange)
        XCTAssertEqual(calendar.callout, .failed)

        let cancelled = await calendar.connect { _, _ in throw CancellationError() }
        XCTAssertFalse(cancelled)
        XCTAssertNil(calendar.callout, "cancelling the browser sheet is not an error")

        let garbage = await calendar.connect { _, _ in URL(string: "reclaimmytime://elsewhere")! }
        XCTAssertFalse(garbage)
        XCTAssertEqual(calendar.callout, .failed)

        let retired = await calendar.connect { _, _ in URL(string: "reclaimmytime://gcal/callback?result=connected")! }
        XCTAssertFalse(retired, "result=connected no longer exists")
        XCTAssertEqual(calendar.callout, .failed)

        let posts = await h.transport.requests(method: "POST", path: "/gcal/complete")
        XCTAssertTrue(posts.isEmpty)
    }

    func testStateInvalidFixtureShowsCallout() async throws {
        let h = try T52Harness()
        await h.mock.setVariant("state_invalid", for: "completeGcalConnect")
        let connected = await h.model.calendar.connect { _, _ in Self.authorized() }
        XCTAssertFalse(connected)
        XCTAssertEqual(h.model.calendar.callout, .failed)
        XCTAssertNotEqual(h.model.calendar.status, .connected)
    }

    func testMockAuthenticatorCompletesAgainstMockTransport() async throws {
        let h = try T52Harness()
        let connected = await h.model.calendar.connect { url, _ in
            MockWebAuthenticator.callbackURL(authorizationURL: url, arguments: [])
        }
        XCTAssertTrue(connected, "mock mode: authorized callback whose state matches the connect fixture")
        XCTAssertEqual(h.model.calendar.status, .connected)
    }

    func testMockAuthenticatorUsesConnectFixtureStateAndErrorFlag() throws {
        let opened = try XCTUnwrap(URL(string: "https://accounts.google.com/o/oauth2/v2/auth?state=other"))
        let fixtureState = MockWebAuthenticator.connectFixtureState()
        XCTAssertEqual(fixtureState, Self.fixtureState)
        XCTAssertEqual(GcalCallbackResult(url: MockWebAuthenticator.callbackURL(authorizationURL: opened, arguments: [], fixtureState: fixtureState)),
                       .authorized(authCode: MockWebAuthenticator.fixtureAuthCode, state: Self.fixtureState))
        XCTAssertEqual(GcalCallbackResult(url: MockWebAuthenticator.callbackURL(authorizationURL: opened, arguments: [], fixtureState: nil)),
                       .authorized(authCode: MockWebAuthenticator.fixtureAuthCode, state: "other"), "falls back to the opened URL's state")
        XCTAssertEqual(GcalCallbackResult(url: MockWebAuthenticator.callbackURL(authorizationURL: opened, arguments: ["-MockGcalResult", "gcal_access_denied"])),
                       .error(code: "gcal_access_denied"))
    }

    func testMockTransportServesGcalComplete() async throws {
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        var request = URLRequest(url: URL(string: "\(AppConfig.mockBaseURL)/gcal/complete")!)
        request.httpMethod = "POST"
        let resolved = await mock.resolve(request)
        let fixture = try XCTUnwrap(resolved)
        XCTAssertEqual(fixture.status, 200)
        let status = try JSONCoding.makeDecoder().decode(GcalStatus.self, from: XCTUnwrap(fixture.responseBody))
        XCTAssertEqual(status.connection?.status, .active)
    }

    /// The callback must only reach the app through its own ASWebAuthenticationSession: the scheme
    /// must never be registered as a URL type.
    func testCallbackSchemeIsNotRegisteredAsURLType() {
        let types = Bundle.main.object(forInfoDictionaryKey: "CFBundleURLTypes") as? [[String: Any]] ?? []
        let schemes = types.flatMap { $0["CFBundleURLSchemes"] as? [String] ?? [] }
        XCTAssertFalse(schemes.contains(GcalCallbackResult.scheme))
    }

    private static func calendarModel(transport: any HTTPTransport) throws -> CalendarConnectionModel {
        let (store, _) = makeTestSessionStore()
        let suite = "gcal.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        let model = AppModel(config: AppConfig(backend: .mock), transport: transport, sessionStore: store, defaults: defaults)
        return model.calendar
    }

    func testNotNowPersistsNotConnectedAndConsentIsPerUser() throws {
        let h = try T52Harness()
        let user = UUID()
        XCTAssertTrue(h.model.calendar.needsConsentStep(userId: user))
        h.model.calendar.activate(userId: user)
        h.model.calendar.skip()
        h.model.calendar.completeConsentStep(userId: user)

        // Status is persisted per account and backend (T5.2 review finding).
        let reloaded = CalendarConnectionModel(api: h.model.api, defaults: h.defaults, scope: h.model.calendar.scope)
        reloaded.activate(userId: user)
        XCTAssertEqual(reloaded.status, .notConnected, "Settings reads the persisted 'Calendar not connected' state")
        XCTAssertFalse(reloaded.needsConsentStep(userId: user))
        XCTAssertTrue(reloaded.needsConsentStep(userId: UUID()))
    }

    func testRefreshStatusFollowsServer() async throws {
        let h = try T52Harness()
        await h.mock.setVariant("not_connected", for: "getGcalStatus")
        await h.model.calendar.refreshStatus()
        XCTAssertEqual(h.model.calendar.status, .notConnected)
        await h.mock.setVariant("revoked", for: "getGcalStatus")
        await h.model.calendar.refreshStatus()
        XCTAssertEqual(h.model.calendar.status, .revoked)
        await h.mock.setVariant("connected", for: "getGcalStatus")
        await h.model.calendar.refreshStatus()
        XCTAssertEqual(h.model.calendar.status, .connected)
        XCTAssertNotNil(h.model.calendar.connectedEmail)
    }
}

/// Fixture mock with programmable `GET /gcal/connect` / `POST /gcal/complete` replies; records requests.
actor GcalStubTransport: HTTPTransport {
    let inner: MockTransport
    private(set) var requests: [URLRequest] = []
    private var completeFailure: (status: Int, code: String)?
    private var connectURL: String?

    init(inner: MockTransport) {
        self.inner = inner
    }

    func failComplete(status: Int, code: String) {
        completeFailure = (status, code)
    }

    func overrideConnect(authorizationURL: String) {
        connectURL = authorizationURL
    }

    func requests(method: String, path: String) -> [URLRequest] {
        requests.filter { $0.httpMethod == method && $0.url?.path() == path }
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        let url = try XCTUnwrap(request.url)
        if request.httpMethod == "POST", url.path() == "/gcal/complete", let failure = completeFailure {
            let body = try JSONCoding.makeEncoder().encode(ErrorEnvelope(error: ErrorBody(code: failure.code, message: "stub")))
            return (body, try Self.response(url, failure.status))
        }
        if request.httpMethod == "GET", url.path() == "/gcal/connect", let connectURL {
            return (Data(#"{"authorization_url":"\#(connectURL)"}"#.utf8), try Self.response(url, 200))
        }
        return try await inner.data(for: request)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        requests.append(request)
        return try await inner.stream(for: request)
    }

    private static func response(_ url: URL, _ status: Int) throws -> HTTPURLResponse {
        try XCTUnwrap(HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"]))
    }
}
