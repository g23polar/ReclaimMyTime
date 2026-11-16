import XCTest
@testable import ReclaimMyTime

/// T4.3 iOS follow-up: `Changeset.reason`, `POST /gcal/sync` (foreground, background, drift
/// alerts, O2 recovery), and 409 `gcal_reconnect_required` / 503 `gcal_unavailable` on solving
/// routes (NOTES D25 and the error table).
enum GcalSyncSupport {
    static let decoder = JSONCoding.makeDecoder()

    /// A fixture changeset's JSON with fields overridden (e.g. `reason`, `staged`).
    static func changeset(_ file: String, syncFixture: Bool = false, _ overrides: [String: Any]) throws -> Data {
        var body = try T53Bodies.fixtureBody(file)
        if syncFixture { body = try XCTUnwrap(body["pending_changeset"] as? [String: Any]) }
        for (key, value) in overrides { body[key] = value }
        return try T53Bodies.data(body)
    }

    static func decodeChangeset(_ data: Data) throws -> Changeset {
        try decoder.decode(Changeset.self, from: data)
    }

    /// The drift fixture's changeset (empty `staged`).
    static func drift() throws -> Changeset {
        try T52Fixtures.syncChangeset("post_gcal_sync__pending_drift.json")
    }

    /// The drift fixture re-labelled `google_edit` with the given staged mutations (T4.3b allows none).
    static func googleEdit(staged: [Any] = []) throws -> Changeset {
        try decodeChangeset(changeset("post_gcal_sync__pending_drift.json", syncFixture: true, ["reason": "google_edit", "staged": staged, "id": UUID().uuidString]))
    }

    /// A staged `pin_added` (as JSON) for `project` at `start`…`end`.
    static func pinMutation(project: UUID, start: String, end: String) -> [String: Any] {
        [
            "kind": "pin_added", "source": "system", "interpretation": NSNull(),
            "payload": ["pin_id": UUID().uuidString, "project_id": project.uuidString,
                        "span": ["start": start, "end": end], "kind": "pin", "created_from_block_id": NSNull()],
        ]
    }

    /// `GcalSyncResult` JSON carrying `changeset` (or null).
    static func syncBody(_ changeset: Changeset?) throws -> Data {
        let pending: Any = try changeset.map { try JSONSerialization.jsonObject(with: JSONCoding.makeEncoder().encode($0)) } ?? NSNull()
        return try T53Bodies.data(["pending_changeset": pending, "outbox": ["pending": 0, "synced": 9]])
    }

    /// The fixture plan with lineage `deleted` gone and lineage `moved` shifted an hour later:
    /// what `GET /plans/current` returns after sync applied a Google-side edit directly.
    static func planAfterDirectEdit(deleted: String, moved: String) throws -> Data {
        let iso = ISO8601DateFormatter()
        let blocks = try XCTUnwrap(T53Bodies.fixtureBody("get_plans_current__ok.json")["blocks"] as? [[String: Any]])
        let edited = blocks.compactMap { block -> [String: Any]? in
            let lineage = (block["lineage_id"] as? String) ?? ""
            if lineage.hasSuffix(deleted) { return nil }
            guard lineage.hasSuffix(moved), var span = block["span"] as? [String: String],
                  let start = span["start"].flatMap(iso.date(from:)), let end = span["end"].flatMap(iso.date(from:))
            else { return block }
            span["start"] = iso.string(from: start.addingTimeInterval(3600))
            span["end"] = iso.string(from: end.addingTimeInterval(3600))
            var block = block
            block["span"] = span
            return block
        }
        return try T53Bodies.plan(blocks: edited)
    }

    /// A user-edit changeset (the drag's staged pin).
    static func userEdit() throws -> Changeset {
        try T52Fixtures.changeset("patch_blocks_id__edit_pin.json")
    }

    static func errorBody(_ code: String) throws -> Data {
        try T53Bodies.data(["error": ["code": code, "message": "fixture"]])
    }

    /// An agent SSE fixture whose stream ends in `error` with `code` (in-stream, after a delta).
    static func agentErrorFixture(code: String) throws -> Fixture {
        let base = try XCTUnwrap(TestFixtures.all().first { $0.operation == "postAgentMessage" && $0.caseName == "laptop" })
        let frames: [(String, [String: Any])] = [
            ("delta", ["text": "Let me check your week."]),
            ("error", ["error": ["code": code, "message": "fixture"]]),
        ]
        let events = try frames.map { Fixture.StreamEvent(event: $0.0, data: try JSONSerialization.data(withJSONObject: $0.1)) }
        let body = try JSONSerialization.data(withJSONObject: frames.map { ["event": $0.0, "data": $0.1] })
        return Fixture(
            fileName: "synthetic_\(code).json", operation: base.operation, method: base.method, path: base.path,
            caseName: code, requestValid: true, responseValid: true, pathParams: [:], query: [:],
            requestHeaders: [:], requestBody: nil, status: 200, responseHeaders: [:], isEventStream: true,
            responseBody: body, streamEvents: events
        )
    }

    /// A signed-in AppModel whose calendar model is bound to the account (RootView does this).
    @MainActor
    static func model(transport: any HTTPTransport, drift: (any DriftSource)? = nil, center: FakeNotificationCenter = FakeNotificationCenter(), defaults: UserDefaults = T54.freshDefaults()) async throws -> AppModel {
        let model = await T54.model(transport: transport, center: center, drift: drift, defaults: defaults)
        guard case .signedIn(let session) = model.auth.phase else {
            XCTFail("not signed in")
            return model
        }
        model.calendar.activate(userId: session.userId)
        return model
    }
}

// MARK: - reason decoding

final class ChangesetReasonDecodingTests: XCTestCase {
    func testEveryReasonValueDecodes() throws {
        let cases: [(String, ChangesetReason)] = [("user_edit", .userEdit), ("agent", .agent), ("drift", .drift), ("google_edit", .googleEdit)]
        for (raw, expected) in cases {
            let data = try GcalSyncSupport.changeset("patch_blocks_id__edit_pin.json", ["reason": raw])
            XCTAssertEqual(try GcalSyncSupport.decodeChangeset(data).reason, expected, raw)
        }
    }

    func testFixturesCarryTheirReason() throws {
        XCTAssertEqual(try GcalSyncSupport.userEdit().reason, .userEdit)
        XCTAssertEqual(try T52Fixtures.agentDone("post_agent_message__laptop.json").changeset?.reason, .agent)
        XCTAssertEqual(try GcalSyncSupport.drift().reason, .drift)
    }

    func testUnknownOrMissingReasonFails() throws {
        let unknown = try GcalSyncSupport.changeset("patch_blocks_id__edit_pin.json", ["reason": "system"])
        XCTAssertThrowsError(try GcalSyncSupport.decodeChangeset(unknown), "strict while the contract is frozen")
        var body = try T53Bodies.fixtureBody("patch_blocks_id__edit_pin.json")
        body.removeValue(forKey: "reason")
        XCTAssertThrowsError(try GcalSyncSupport.decodeChangeset(T53Bodies.data(body)), "reason is required")
    }

    func testDriftDecodesWithEmptyStaged() throws {
        let drift = try GcalSyncSupport.drift()
        XCTAssertTrue(drift.staged.isEmpty)
        XCTAssertNotNil(drift.plan)
        XCTAssertNotNil(drift.diff)
    }

    func testGoogleEditDecodesWithEmptyStaged() throws {
        // T4.3b: the Google move was applied directly; only the knock-on re-plan is proposed.
        let edit = try GcalSyncSupport.googleEdit()
        XCTAssertEqual(edit.reason, .googleEdit)
        XCTAssertTrue(edit.staged.isEmpty)
        XCTAssertNotNil(DiffModel(edit), "the diff view renders it like drift")
    }

    func testEmptyStagedIsRejectedForUserEditAndAgent() throws {
        for reason in ["user_edit", "agent"] {
            let data = try GcalSyncSupport.changeset("patch_blocks_id__edit_pin.json", ["reason": reason, "staged": []])
            XCTAssertThrowsError(try GcalSyncSupport.decodeChangeset(data), reason)
        }
    }

    func testReasonRoundTrips() throws {
        let drift = try GcalSyncSupport.drift()
        let encoded = try JSONCoding.makeEncoder().encode(drift)
        XCTAssertEqual(try GcalSyncSupport.decodeChangeset(encoded), drift)
    }

    func testSyncResultDecodesEveryFixture() throws {
        let decoder = JSONCoding.makeDecoder()
        let notConnected = try decoder.decode(GcalSyncResult.self, from: XCTUnwrap(TestFixtures.named("post_gcal_sync__not_connected.json").responseBody))
        XCTAssertNil(notConnected.pendingChangeset)
        XCTAssertEqual(notConnected.outbox, OutboxCounts(pending: 0, synced: 0))
        let drift = try decoder.decode(GcalSyncResult.self, from: XCTUnwrap(TestFixtures.named("post_gcal_sync__pending_drift.json").responseBody))
        XCTAssertEqual(drift.pendingChangeset?.reason, .drift)
    }
}

// MARK: - client

final class SyncGcalClientTests: XCTestCase {
    private var sessionStore: SessionStore!

    override func setUp() async throws {
        (sessionStore, _) = makeTestSessionStore()
        StubURLProtocol.reset()
    }

    override func tearDown() async throws {
        await sessionStore.clear()
        StubURLProtocol.reset()
    }

    private func makeClient(onReconnect: (@Sendable (String?) async -> Void)? = nil) -> URLSessionAPIClient {
        URLSessionAPIClient(
            baseURL: URL(string: "https://api.example.test/v1")!,
            transport: URLSessionTransport(session: StubURLProtocol.makeSession()),
            sessionStore: sessionStore,
            timeZoneIdentifier: { "Europe/Berlin" },
            onGcalReconnectRequired: onReconnect
        )
    }

    func testSyncGcalRequestShape() async throws {
        let auth = try TestFixtures.authSession()
        try await sessionStore.save(auth)
        let payload = try XCTUnwrap(TestFixtures.named("post_gcal_sync__not_connected.json").responseBody)
        StubURLProtocol.install { _, _ in StubURLProtocol.Reply(status: 200, chunks: [payload]) }

        let result = try await makeClient().syncGcal()

        XCTAssertNil(result.pendingChangeset)
        let (sent, body) = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(sent.url?.absoluteString, "https://api.example.test/v1/gcal/sync")
        XCTAssertEqual(sent.httpMethod, "POST")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Authorization"), "Bearer \(auth.token)")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "X-Timezone"), "Europe/Berlin")
        XCTAssertNil(sent.value(forHTTPHeaderField: "Content-Type"), "no body")
        XCTAssertTrue(body?.isEmpty ?? true, "no body")
    }

    func testReconnectHookFiresForSolvingRoutesButNotForSync() async throws {
        let auth = try TestFixtures.authSession()
        try await sessionStore.save(auth)
        let reconnect = try XCTUnwrap(TestFixtures.named("post_projects__gcal_reconnect_required.json").responseBody)
        StubURLProtocol.install { _, _ in StubURLProtocol.Reply(status: 409, chunks: [reconnect]) }
        let counter = Counter()
        let client = makeClient { token in await counter.bump(token) }

        do {
            _ = try await client.createProject(ProjectDraft(name: "Grant", goal: .perennial(weeklyTargetHours: 2), maxBlockMinutes: 60, requirements: []))
            XCTFail("expected 409")
        } catch {
            XCTAssertEqual(GcalSolveFailure(error), .reconnectRequired)
        }
        var fired = await counter.value
        XCTAssertEqual(fired, 1)
        let tokens = await counter.tokens
        XCTAssertEqual(tokens, [auth.token], "the hook gets the bearer the refused request carried")

        do {
            _ = try await client.syncGcal()
            XCTFail("expected 409")
        } catch {
            XCTAssertEqual(GcalSolveFailure(error), .reconnectRequired)
        }
        fired = await counter.value
        XCTAssertEqual(fired, 1, "sync announces its own 409 once per revocation")
    }
}

actor Counter {
    private(set) var value = 0
    private(set) var tokens: [String?] = []
    func bump(_ token: String? = nil) {
        value += 1
        tokens.append(token)
    }
}

// MARK: - foreground / background

@MainActor
final class GcalSyncAppTests: XCTestCase {
    private var gate: GatedTransport!

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
    }

    private func syncCount() async -> Int {
        await gate.requests.filter { $0.httpMethod == "POST" && $0.url?.path() == "/gcal/sync" }.count
    }

    func testForegroundTriggersSync() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await model.handleForeground()
        let count = await syncCount()
        XCTAssertEqual(count, 1)
    }

    func testForegroundSyncDoesNotBlockTheTimetable() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.hold("POST", "/gcal/sync")
        let foreground = Task { await model.handleForeground() }
        await waitUntil { model.timetable.plan != nil }
        let waiting = await gate.waitingCount("POST", "/gcal/sync")
        XCTAssertEqual(waiting, 1, "the week loaded while the sync was still in flight")
        await gate.release("POST", "/gcal/sync")
        await foreground.value
    }

    func testConcurrentSyncsShareOneRequest() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.hold("POST", "/gcal/sync")
        async let first = model.syncGcal()
        async let second = model.syncGcal()
        await waitUntil { await self.gate.waitingCount("POST", "/gcal/sync") == 1 }
        await gate.release("POST", "/gcal/sync")
        _ = await (first, second)
        let count = await syncCount()
        XCTAssertEqual(count, 1)
    }

    func testForegroundSyncRecoversThePendingChangesetIntoTheBanner() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/gcal/sync", body: try XCTUnwrap(TestFixtures.named("post_gcal_sync__pending_drift.json").responseBody))
        await model.handleForeground()
        let drift = try GcalSyncSupport.drift()
        XCTAssertEqual(model.pending.pending?.id, drift.id, "O2: a relaunch recovers the pending changeset")
        XCTAssertEqual(ProposalSummary.make(try XCTUnwrap(model.pending.pending)).summary, "Your calendar changed")
        let confirms = await gate.requests.filter { $0.url?.path().hasSuffix("/confirm") == true }.count
        XCTAssertEqual(confirms, 0, "never applied")
        XCTAssertFalse(model.navigation.isReviewingProposal, "foreground drift waits in the banner")
    }

    func testSyncReconnectRequiredShowsTheBannerOncePerRevocation() async throws {
        let defaults = T54.freshDefaults()
        let model = try await GcalSyncSupport.model(transport: gate, defaults: defaults)
        let body = try XCTUnwrap(TestFixtures.named("post_gcal_sync__reconnect_required.json").responseBody)
        await gate.respond("POST", "/gcal/sync", status: 409, body: body)
        await model.syncGcal()
        XCTAssertTrue(model.calendar.showsRevokedBanner)
        XCTAssertEqual(model.calendar.status, .revoked)
        XCTAssertTrue(model.calendar.needsReconnect)
        model.calendar.dismissRevokedBanner()

        await gate.respond("POST", "/gcal/sync", status: 409, body: body)
        await model.syncGcal()
        XCTAssertFalse(model.calendar.showsRevokedBanner, "the next foreground doesn't nag")
    }

    func testReconnectSurvivesAStatusReadThatStillSaysActive() async throws {
        // An undecryptable credential: GET /gcal/status is `active`, solves still answer 409.
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/gcal/sync", status: 409, body: try GcalSyncSupport.errorBody(KnownErrorCode.gcalReconnectRequired))
        await model.handleForeground()
        XCTAssertEqual(model.calendar.status, .revoked)
        XCTAssertTrue(model.calendar.showsRevokedBanner)

        await model.handleForeground()  // fixture sync 200 now
        XCTAssertFalse(model.calendar.needsReconnect)
        await model.calendar.refreshStatus()
        XCTAssertEqual(model.calendar.status, .connected)
    }

    func testSyncUnavailableIsSilentAndRetriedNextForeground() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/gcal/sync", status: 503, body: try XCTUnwrap(TestFixtures.named("post_gcal_sync__unavailable.json").responseBody))
        let result = await model.syncGcal()
        XCTAssertNil(result)
        XCTAssertFalse(model.calendar.showsRevokedBanner)
        XCTAssertNil(model.pending.notice)
        await model.handleForeground()
        let count = await syncCount()
        XCTAssertEqual(count, 2)
    }

    // A successful sync reloads the week (T4.3b: Google edits can be applied directly).

    private func block(_ model: AppModel, lineage: String) -> Block? {
        model.timetable.plan?.blocks.first { $0.lineageId == T54.lineage(lineage) }
    }

    func testDirectEditSyncReloadsThePlanAndReminders() async throws {
        let center = FakeNotificationCenter()
        let model = try await GcalSyncSupport.model(transport: gate, center: center)
        await model.timetable.load()
        await model.notifications.waitForIdle()
        let before = try XCTUnwrap(block(model, lineage: "04")).span
        var ids = await center.sortedPendingIds
        XCTAssertTrue(ids.contains(BlockNotificationPlanner.startIdentifier(lineage: T54.lineage("03"))))

        await gate.respond("POST", "/gcal/sync", body: try GcalSyncSupport.syncBody(nil))
        await gate.respond("GET", "/plans/current", body: try GcalSyncSupport.planAfterDirectEdit(deleted: "03", moved: "04"))
        let result = await model.syncGcal()
        XCTAssertNotNil(result)
        XCTAssertNil(result?.pendingChangeset, "no proposal: the edit was applied directly")
        await model.notifications.waitForIdle()

        XCTAssertEqual(block(model, lineage: "04")?.span.start, before.start.addingTimeInterval(3600), "the timetable shows the new span")
        XCTAssertNil(block(model, lineage: "03"))
        ids = await center.sortedPendingIds
        XCTAssertFalse(ids.contains(BlockNotificationPlanner.startIdentifier(lineage: T54.lineage("03"))), "the deleted block's reminder is gone")
        XCTAssertNil(model.pending.pending)
    }

    func testAnOlderForegroundLoadCannotOverwriteTheSyncReload() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let original = try T53.plan()
        let movedStart = try XCTUnwrap(original.blocks.first { $0.lineageId == T54.lineage("04") }).span.start.addingTimeInterval(3600)
        // FIFO: the foreground's own load takes the old plan (and is held); the sync's load the new.
        await gate.respond("GET", "/plans/current", body: try T53Bodies.plan())
        await gate.respond("GET", "/plans/current", body: try GcalSyncSupport.planAfterDirectEdit(deleted: "03", moved: "04"))
        await gate.respond("POST", "/gcal/sync", body: try GcalSyncSupport.syncBody(nil))
        await gate.hold("GET", "/plans/current")
        await gate.hold("POST", "/gcal/sync")

        let foreground = Task { await model.handleForeground() }
        await waitUntil {
            let plans = await self.gate.waitingCount("GET", "/plans/current")
            let syncs = await self.gate.waitingCount("POST", "/gcal/sync")
            return plans == 1 && syncs == 1
        }
        await gate.release("POST", "/gcal/sync")
        await waitUntil { self.block(model, lineage: "04")?.span.start == movedStart }
        await gate.release("GET", "/plans/current")
        await foreground.value

        XCTAssertEqual(block(model, lineage: "04")?.span.start, movedStart, "the older load finished last but didn't win")
        XCTAssertNil(block(model, lineage: "03"))
    }

    func testBackgroundRefreshSyncsBeforeLoadingThePlan() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await model.handleBackgroundRefresh()
        let paths = await gate.requests.map { "\($0.httpMethod ?? "") \($0.url?.path() ?? "")" }
        let sync = try XCTUnwrap(paths.firstIndex(of: "POST /gcal/sync"))
        let plan = try XCTUnwrap(paths.firstIndex(of: "GET /plans/current"))
        XCTAssertLessThan(sync, plan, "sync first: it may change the live plan")
        XCTAssertEqual(paths.filter { $0 == "GET /plans/current" }.count, 1, "loaded once, after the sync")
    }

    func testBackgroundRefreshStillLoadsWhenSyncFails() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/gcal/sync", status: 503, body: try GcalSyncSupport.errorBody(KnownErrorCode.gcalUnavailable))
        await model.handleBackgroundRefresh()
        let plans = await gate.requests.filter { $0.url?.path() == "/plans/current" }.count
        XCTAssertEqual(plans, 1, "the reminder window keeps rolling")
        XCTAssertNotNil(model.timetable.plan)
    }

    // Session scoping (review round 2).

    /// Signs out and back in as the same mock user, the way RootView routes it.
    private func reSignIn(_ model: AppModel) async {
        await model.auth.signOut()
        model.handleAuthPhase(model.auth.phase)
        await model.auth.signInWithApple(identityToken: "mock-identity-token", authorizationCode: "mock-authorization-code", rawNonce: Nonce.random())
        model.handleAuthPhase(model.auth.phase)
    }

    func testSyncFromAPreviousSessionIsNotAppliedAfterReSignIn() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/gcal/sync", body: try XCTUnwrap(TestFixtures.named("post_gcal_sync__pending_drift.json").responseBody))
        await gate.hold("POST", "/gcal/sync")
        let stale = Task { await model.syncGcal() }
        await waitUntil { await self.gate.waitingCount("POST", "/gcal/sync") == 1 }

        await reSignIn(model)
        guard case .signedIn = model.auth.phase else { return XCTFail("not signed back in") }
        await gate.release("POST", "/gcal/sync")
        let result = await stale.value

        XCTAssertNil(result, "the old session's answer is dropped")
        XCTAssertNil(model.pending.pending, "same user, new session: nothing from the old sync is staged")
        let plans = await gate.requests.filter { $0.url?.path() == "/plans/current" }.count
        XCTAssertEqual(plans, 0, "and no reload on its behalf")
    }

    func testSignOutDuringTheRebuildPostsNoAlert() async throws {
        let center = FakeNotificationCenter()
        let drift = try GcalSyncSupport.drift()
        let model = try await GcalSyncSupport.model(transport: gate, drift: FakeDriftSource(changeset: drift), center: center)
        await center.holdAdds()
        let refresh = Task { await model.handleBackgroundRefresh() }
        await waitUntil { await center.waitingAdds > 0 }

        await model.auth.signOut()
        model.handleAuthPhase(model.auth.phase)
        await center.releaseAdds()
        await refresh.value
        await model.notifications.waitForIdle()

        let delivered = await center.delivered
        XCTAssertFalse(delivered.contains { $0.identifier == DriftNotification.identifier(drift.id) }, "no drift alert for a signed-out session")
    }

    // An empty sync answer retires an obsolete local proposal (review round 2).

    func testEmptySyncRetiresAProposalAdoptedBeforeIt() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let local = try GcalSyncSupport.userEdit()
        XCTAssertTrue(model.pending.stage(local))
        await gate.respond("POST", "/gcal/sync", body: try GcalSyncSupport.syncBody(nil))
        await model.syncGcal()
        XCTAssertNil(model.pending.pending, "resolved or expired elsewhere: the banner goes")
        XCTAssertEqual(model.pending.lifecycle(of: local.id), .outOfDate)

        // The high-water mark is reset: an older-dated proposal can be adopted again.
        let older = try GcalSyncSupport.decodeChangeset(GcalSyncSupport.changeset(
            "patch_blocks_id__edit_pin.json", ["id": UUID().uuidString, "created_at": "2020-01-01T00:00:00Z"]))
        XCTAssertTrue(model.pending.stage(older))
    }

    func testEmptySyncKeepsAProposalStagedAfterItStarted() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/gcal/sync", body: try GcalSyncSupport.syncBody(nil))
        await gate.hold("POST", "/gcal/sync")
        let sync = Task { await model.syncGcal() }
        await waitUntil { await self.gate.waitingCount("POST", "/gcal/sync") == 1 }
        let local = try GcalSyncSupport.userEdit()
        XCTAssertTrue(model.pending.stage(local), "a write staged while the sync is in flight")
        await gate.release("POST", "/gcal/sync")
        _ = await sync.value
        XCTAssertEqual(model.pending.pending?.id, local.id, "newer than the server's answer: kept")
    }

    // Cold start (review round 2).

    func testActivationBeforeBootstrapSyncsOnceAfterSignIn() async throws {
        let (store, _) = makeTestSessionStore()
        let model = AppModel(config: AppConfig(backend: .mock), transport: gate, sessionStore: store, defaults: T54.freshDefaults(), notificationCenter: FakeNotificationCenter(), backgroundScheduler: FakeBackgroundScheduler())
        model.handleScenePhase(.active)  // before auth has a session
        try await Task.sleep(for: .milliseconds(50))
        var syncs = await syncCount()
        XCTAssertEqual(syncs, 0, "nothing to sync while signed out")

        await model.auth.signInWithApple(identityToken: "mock-identity-token", authorizationCode: "mock-authorization-code", rawNonce: Nonce.random())
        model.handleAuthPhase(model.auth.phase)
        model.handleAuthPhase(model.auth.phase)  // RootView's onChange may report it again
        await waitUntil { await self.syncCount() == 1 }
        try await Task.sleep(for: .milliseconds(100))
        syncs = await syncCount()
        XCTAssertEqual(syncs, 1, "one foreground pass for the session")
    }

    // Background refresh → DriftAlert.

    private func backgroundDelivered(_ changeset: Changeset?) async throws -> [LocalNotificationRequest] {
        let center = FakeNotificationCenter()
        let model = try await GcalSyncSupport.model(transport: gate, drift: FakeDriftSource(changeset: changeset), center: center)
        await model.handleBackgroundRefresh()
        return await center.delivered
    }

    func testBackgroundGoogleEditPostsItsOwnCopy() async throws {
        let edit = try GcalSyncSupport.googleEdit()
        let delivered = try await backgroundDelivered(edit)
        XCTAssertEqual(delivered.map(\.identifier), [DriftNotification.identifier(edit.id)])
        XCTAssertEqual(delivered.first?.title, "You moved a block in Google Calendar")
        // Empty staged: the edit is already live; the diff's `after` spans are only proposals.
        XCTAssertEqual(delivered.first?.body, "Your change is in. Tap to review how the rest of your week fits.")
    }

    func testBackgroundGoogleEditWithAPinNamesThePinnedBlock() async throws {
        let edit = try GcalSyncSupport.googleEdit(staged: [GcalSyncSupport.pinMutation(project: T52Fixtures.workoutId, start: "2026-11-19T15:00:00Z", end: "2026-11-19T16:00:00Z")])
        let delivered = try await backgroundDelivered(edit)
        XCTAssertEqual(T53.plain(delivered.first?.body ?? ""), "Workout is now at 7:00 AM. Tap to review how the rest of your week fits.")
    }

    func testBackgroundUserEditOrNothingPostsNothing() async throws {
        var delivered = try await backgroundDelivered(try GcalSyncSupport.userEdit())
        XCTAssertTrue(delivered.isEmpty, "user_edit: the user just made it")
        let agent = try XCTUnwrap(T52Fixtures.agentDone("post_agent_message__laptop.json").changeset)
        delivered = try await backgroundDelivered(agent)
        XCTAssertTrue(delivered.isEmpty, "agent")
        delivered = try await backgroundDelivered(nil)
        XCTAssertTrue(delivered.isEmpty, "null pending_changeset")
    }

    func testBackgroundSyncFailurePostsNothing() async throws {
        let center = FakeNotificationCenter()
        let source = FakeDriftSource(changeset: nil, error: .server(code: KnownErrorCode.gcalUnavailable, message: "", details: nil, status: 503))
        let model = try await GcalSyncSupport.model(transport: gate, drift: source, center: center)
        await model.handleBackgroundRefresh()
        let delivered = await center.delivered
        XCTAssertTrue(delivered.isEmpty)
        XCTAssertEqual(source.calls, 1)
    }

    func testDriftTapFromColdStartRecoversAndOpensReview() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/gcal/sync", body: try XCTUnwrap(TestFixtures.named("post_gcal_sync__pending_drift.json").responseBody))
        let drift = try GcalSyncSupport.drift()
        XCTAssertNil(model.pending.pending)
        await model.handle(.drift(changesetId: drift.id))
        XCTAssertEqual(model.pending.pending?.id, drift.id)
        XCTAssertTrue(model.navigation.isReviewingProposal)
    }
}

// MARK: - DriftAlert mapping (pure)

final class DriftAlertTests: XCTestCase {
    func testDriftNamesTheEarliestClashingBlock() throws {
        let drift = try GcalSyncSupport.drift()
        let alert = try XCTUnwrap(DriftAlert(changeset: drift))
        XCTAssertEqual(alert.kind, .drift)
        XCTAssertEqual(alert.projectId, T52Fixtures.workoutId)
        XCTAssertEqual(alert.blockStart, T53.date("2026-11-18T15:00:00Z"), "where the block was (the clash)")
    }

    func testGoogleEditPrefersTheStagedPin() throws {
        let pin = GcalSyncSupport.pinMutation(project: T52Fixtures.portfolioId, start: "2026-11-19T22:00:00Z", end: "2026-11-19T23:00:00Z")
        let edit = try GcalSyncSupport.googleEdit(staged: [pin])
        let alert = try XCTUnwrap(DriftAlert(changeset: edit))
        XCTAssertEqual(alert.kind, .googleEdit)
        XCTAssertEqual(alert.projectId, T52Fixtures.portfolioId)
        XCTAssertEqual(alert.blockStart, T53.date("2026-11-19T22:00:00Z"))
    }

    func testAppliedGoogleEditNamesNoBlock() throws {
        let alert = try XCTUnwrap(DriftAlert(changeset: GcalSyncSupport.googleEdit()))
        XCTAssertEqual(alert.kind, .googleEditApplied)
        XCTAssertNil(alert.projectId)
        XCTAssertNil(alert.blockStart)
        let request = DriftNotification.request(alert, projectName: "", time: T53.time)
        XCTAssertEqual(request.title, "You moved a block in Google Calendar")
        XCTAssertEqual(request.body, "Your change is in. Tap to review how the rest of your week fits.")
    }

    func testNoAlertForUserEditAgentOrResolved() throws {
        XCTAssertNil(DriftAlert(changeset: try GcalSyncSupport.userEdit()))
        XCTAssertNil(DriftAlert(changeset: try XCTUnwrap(T52Fixtures.agentDone("post_agent_message__laptop.json").changeset)))
        let confirmed = try GcalSyncSupport.decodeChangeset(GcalSyncSupport.changeset("post_gcal_sync__pending_drift.json", syncFixture: true, ["status": "confirmed", "resolved_at": "2026-11-17T19:00:00Z"]))
        XCTAssertNil(DriftAlert(changeset: confirmed))
    }

    func testDriftNotificationCopyStillMatchesDesign() throws {
        let request = DriftNotification.request(try XCTUnwrap(DriftAlert(changeset: GcalSyncSupport.drift())), projectName: "Workout", time: T53.time)
        XCTAssertEqual(request.title, "A meeting landed on your plan")
        XCTAssertEqual(request.categoryIdentifier, NotificationCategoryID.drift)
    }
}

// MARK: - copy

final class ChangesetReasonCopyTests: XCTestCase {
    func testBannerCopyPerReason() throws {
        XCTAssertEqual(ProposalSummary.make(try GcalSyncSupport.drift()).summary, "Your calendar changed")
        XCTAssertEqual(ProposalSummary.make(try GcalSyncSupport.googleEdit()).summary, "You moved a block in Google Calendar")
        XCTAssertEqual(ProposalSummary.make(try GcalSyncSupport.userEdit()).summary, "1 block moved", "user_edit keeps the diff counts")
        XCTAssertNil(ChangesetReasonCopy.headline(.agent))
        XCTAssertNil(ChangesetReasonCopy.headline(.userEdit))
    }

    func testRejectCopyForGoogleEdit() throws {
        let applied = try GcalSyncSupport.googleEdit()
        XCTAssertEqual(ProposalReviewCopy.rejectTitle(applied), "Keep the rest of my week", "the Google edit stays")
        XCTAssertNil(ProposalReviewCopy.rejectFootnote(applied))
        let pinned = try GcalSyncSupport.googleEdit(staged: [GcalSyncSupport.pinMutation(project: T52Fixtures.workoutId, start: "2026-11-19T15:00:00Z", end: "2026-11-19T16:00:00Z")])
        XCTAssertEqual(ProposalReviewCopy.rejectTitle(pinned), "Keep original plan")
        XCTAssertEqual(ProposalReviewCopy.rejectFootnote(pinned), "Moves the edited events back in Google Calendar. Changes already applied stay as they are.")
        let drift = try GcalSyncSupport.drift()
        XCTAssertEqual(ProposalReviewCopy.rejectTitle(drift), "Keep my week")
        XCTAssertNil(ProposalReviewCopy.rejectFootnote(drift))
    }

    func testSolveFailureCopy() {
        XCTAssertEqual(GcalSolveFailure.unavailable.message, "Couldn't reach Google Calendar. Nothing was changed, try again.")
        XCTAssertEqual(GcalSolveFailure(code: "gcal_reconnect_required"), .reconnectRequired)
        XCTAssertEqual(GcalSolveFailure(code: "gcal_unavailable"), .unavailable)
        XCTAssertNil(GcalSolveFailure(code: "agent_unavailable"))
    }
}

// MARK: - 409 / 503 on solving routes

@MainActor
final class GcalSolveErrorTests: XCTestCase {
    private var gate: GatedTransport!

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
    }

    private static let draft = ProjectDraft(name: "Grant application", goal: .deadline(targetDate: LocalDate("2026-11-30")!, estimatedTotalHours: 20), maxBlockMinutes: 120, minBlockMinutes: 30, requirements: ["laptop"])

    func testProjectCreateReconnectRaisesTheBanner() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/projects", status: 409, body: try XCTUnwrap(TestFixtures.named("post_projects__gcal_reconnect_required.json").responseBody))
        do {
            _ = try await model.api.createProject(Self.draft)
            XCTFail("expected 409")
        } catch {
            XCTAssertEqual(GcalSolveFailure(error), .reconnectRequired)
        }
        XCTAssertTrue(model.calendar.showsRevokedBanner)
        XCTAssertEqual(model.calendar.status, .revoked)
        XCTAssertNil(model.pending.pending, "nothing staged")
    }

    func testProjectCreateUnavailableIsRetryableWithoutBanner() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/projects", status: 503, body: try XCTUnwrap(TestFixtures.named("post_projects__gcal_unavailable.json").responseBody))
        do {
            _ = try await model.api.createProject(Self.draft)
            XCTFail("expected 503")
        } catch {
            XCTAssertEqual(GcalSolveFailure(error), .unavailable)
        }
        XCTAssertFalse(model.calendar.showsRevokedBanner)
        // Retry succeeds against the default fixture.
        let changeset = try await model.api.createProject(Self.draft)
        XCTAssertEqual(changeset.status, .pending)
    }

    private func stageForConfirm(_ model: AppModel) throws -> Changeset {
        let changeset = try GcalSyncSupport.userEdit()
        XCTAssertTrue(model.pending.stage(changeset))
        return changeset
    }

    func testConfirmReconnectKeepsTheProposalAndRaisesTheBanner() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let changeset = try stageForConfirm(model)
        model.calendar.dismissRevokedBanner()
        await gate.respond("POST", "/changesets/\(changeset.id.pathComponent)/confirm", status: 409, body: try GcalSyncSupport.errorBody(KnownErrorCode.gcalReconnectRequired))
        let confirmed = await model.pending.confirm(id: changeset.id)
        XCTAssertFalse(confirmed)
        XCTAssertEqual(model.pending.pending?.id, changeset.id, "still pending: nothing applied")
        XCTAssertEqual(model.pending.actionError, GcalSolveFailure.reconnectRequired.message)
        XCTAssertTrue(model.calendar.showsRevokedBanner)
    }

    func testConfirmUnavailableShowsRetryableError() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let changeset = try stageForConfirm(model)
        await gate.respond("POST", "/changesets/\(changeset.id.pathComponent)/confirm", status: 503, body: try GcalSyncSupport.errorBody(KnownErrorCode.gcalUnavailable))
        let failed = await model.pending.confirm(id: changeset.id)
        XCTAssertFalse(failed)
        XCTAssertEqual(model.pending.actionError, "Couldn't reach Google Calendar. Nothing was changed, try again.")
        XCTAssertFalse(model.calendar.showsRevokedBanner)
        let retried = await model.pending.confirm(id: changeset.id)
        XCTAssertTrue(retried, "retry against the default fixture succeeds")
    }

    func testConfirmStaleFetchesTheFreshProposalThroughSync() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let changeset = try stageForConfirm(model)
        await gate.respond("POST", "/changesets/\(changeset.id.pathComponent)/confirm", status: 409, body: try GcalSyncSupport.errorBody(KnownErrorCode.changesetStale))
        let fresh = try GcalSyncSupport.decodeChangeset(GcalSyncSupport.changeset(
            "patch_blocks_id__edit_pin.json", ["id": UUID().uuidString, "created_at": "2030-01-01T00:00:00Z"]))
        let syncBody = try T53Bodies.data(["pending_changeset": try JSONSerialization.jsonObject(with: JSONCoding.makeEncoder().encode(fresh)), "outbox": ["pending": 0, "synced": 0]])
        await gate.respond("POST", "/gcal/sync", body: syncBody)
        let confirmed = await model.pending.confirm(id: changeset.id)
        XCTAssertFalse(confirmed)
        await waitUntil { model.pending.pending?.id == fresh.id }
        XCTAssertNil(model.pending.actionError, "the fresh proposal replaces the stale message")
    }

    func testStaleRecoveryStartsAFreshSyncInsteadOfJoiningOneInFlight() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let changeset = try stageForConfirm(model)
        // A sync already in flight answers "nothing pending" from before the stale confirm.
        await gate.respond("POST", "/gcal/sync", body: try GcalSyncSupport.syncBody(nil))
        await gate.hold("POST", "/gcal/sync")
        let earlier = Task { await model.syncGcal() }
        await waitUntil { await self.gate.waitingCount("POST", "/gcal/sync") == 1 }

        let fresh = try GcalSyncSupport.decodeChangeset(GcalSyncSupport.changeset(
            "patch_blocks_id__edit_pin.json", ["id": UUID().uuidString, "created_at": "2030-01-01T00:00:00Z"]))
        await gate.respond("POST", "/gcal/sync", body: try GcalSyncSupport.syncBody(fresh))
        await gate.respond("POST", "/changesets/\(changeset.id.pathComponent)/confirm", status: 409, body: try GcalSyncSupport.errorBody(KnownErrorCode.changesetStale))
        let confirmed = await model.pending.confirm(id: changeset.id)
        XCTAssertFalse(confirmed)
        await waitUntil { model.pending.pending?.id == fresh.id }
        let syncs = await gate.requests.filter { $0.url?.path() == "/gcal/sync" }.count
        XCTAssertEqual(syncs, 2, "a second request, not the in-flight one")
        XCTAssertNil(model.pending.actionError)

        await gate.release("POST", "/gcal/sync")
        await earlier.value
        XCTAssertEqual(model.pending.pending?.id, fresh.id, "the older null answer changes nothing")
    }

    func testStaleRecoveryFindingTheSameChangesetClearsTheFetchingMessage() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let changeset = try stageForConfirm(model)
        await gate.respond("POST", "/changesets/\(changeset.id.pathComponent)/confirm", status: 409, body: try GcalSyncSupport.errorBody(KnownErrorCode.changesetStale))
        await gate.respond("POST", "/gcal/sync", body: try GcalSyncSupport.syncBody(changeset))
        await model.pending.confirm(id: changeset.id)
        await waitUntil { model.pending.actionError == nil }
        XCTAssertEqual(model.pending.pending?.id, changeset.id)
    }

    func testSameIdRestageKeepsOtherErrors() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        let changeset = try stageForConfirm(model)
        await gate.respond("POST", "/changesets/\(changeset.id.pathComponent)/confirm", status: 503, body: try GcalSyncSupport.errorBody(KnownErrorCode.gcalUnavailable))
        await model.pending.confirm(id: changeset.id)
        XCTAssertTrue(model.pending.stage(changeset))
        XCTAssertEqual(model.pending.actionError, GcalSolveFailure.unavailable.message)
    }

    func testReconnectRelayIgnoresAnotherSessionsToken() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("not signed in") }
        let relay = GcalReconnectRelay()
        relay.calendar = model.calendar
        relay.auth = model.auth
        relay.notify(token: "a-previous-accounts-token")
        relay.notify(token: nil)
        XCTAssertFalse(model.calendar.showsRevokedBanner)
        XCTAssertFalse(model.calendar.needsReconnect)
        relay.notify(token: session.token)
        XCTAssertTrue(model.calendar.showsRevokedBanner)
    }

    func testLateReconnectAfterSignOutChangesNothing() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await gate.respond("POST", "/projects", status: 409, body: try GcalSyncSupport.errorBody(KnownErrorCode.gcalReconnectRequired))
        await gate.hold("POST", "/projects")
        let api = model.api
        let draft = Self.draft
        let write = Task { try? await api.createProject(draft) }
        await waitUntil { await self.gate.waitingCount("POST", "/projects") == 1 }
        await model.auth.signOut()
        await gate.release("POST", "/projects")
        _ = await write.value
        XCTAssertFalse(model.calendar.showsRevokedBanner)
        XCTAssertFalse(model.calendar.needsReconnect)
        XCTAssertNotEqual(model.calendar.status, .revoked)
    }

    private func chatHarness(code: String) async throws -> AppModel {
        let fixtures = try TestFixtures.all() + [GcalSyncSupport.agentErrorFixture(code: code)]
        let mock = MockTransport(fixtures: fixtures, frameDelay: .zero, variants: ["postAgentMessage": code])
        return try await GcalSyncSupport.model(transport: mock)
    }

    func testAgentStreamReconnectErrorShowsInlineMessageAndBanner() async throws {
        let model = try await chatHarness(code: KnownErrorCode.gcalReconnectRequired)
        await model.chat.submit("Add a grant application due the 30th.")
        XCTAssertEqual(model.chat.entries.last?.content, .failure(.gcal(.reconnectRequired)))
        XCTAssertTrue(model.chat.hasFailedTurn, "Retry stays available")
        XCTAssertTrue(model.calendar.showsRevokedBanner)
        XCTAssertNil(model.pending.pending, "nothing staged")
    }

    func testAgentStreamUnavailableErrorIsRetryable() async throws {
        let model = try await chatHarness(code: KnownErrorCode.gcalUnavailable)
        await model.chat.submit("Add a grant application due the 30th.")
        XCTAssertEqual(model.chat.entries.last?.content, .failure(.gcal(.unavailable)))
        XCTAssertEqual(ChatModel.TurnFailure.gcal(.unavailable).message, "Couldn't reach Google Calendar. Nothing was changed, try again.")
        XCTAssertTrue(model.chat.hasFailedTurn)
        XCTAssertFalse(model.calendar.showsRevokedBanner)
    }

    func testBlockEditErrorsBecomeTimetableNotices() async throws {
        let model = try await GcalSyncSupport.model(transport: gate)
        await model.timetable.load()
        let block = try XCTUnwrap(model.timetable.plan?.blocks.first { $0.span.start > T53.storyNow })
        let span = try XCTUnwrap(Span(start: block.span.start.addingTimeInterval(3600), end: block.span.end.addingTimeInterval(3600)))
        await gate.respond("PATCH", "/blocks/\(block.id.pathComponent)", status: 503, body: try GcalSyncSupport.errorBody(KnownErrorCode.gcalUnavailable))
        await model.timetable.edit(block, to: span)
        XCTAssertEqual(model.timetable.notice, .gcalUnavailable)
        XCTAssertNil(model.pending.pending)
    }
}

// MARK: - mock mode

final class MockSyncVariantTests: XCTestCase {
    func testMockServesSyncFromFixturesAndHonoursTheDriftVariant() async throws {
        let fixtures = try TestFixtures.all()
        let request = { () -> URLRequest in
            var request = URLRequest(url: URL(string: "https://mock.reclaimmytime.invalid/gcal/sync")!)
            request.httpMethod = "POST"
            return request
        }()
        let plain = MockTransport(fixtures: fixtures, frameDelay: .zero)
        let resolved = await plain.resolve(request)
        XCTAssertEqual(resolved?.operation, "syncGcal")
        XCTAssertLessThan(resolved?.status ?? 999, 400)
        let drift = MockTransport(fixtures: fixtures, frameDelay: .zero, variants: ["syncGcal": "pending_drift"])
        let driftFixture = await drift.resolve(request)
        XCTAssertEqual(driftFixture?.fileName, "post_gcal_sync__pending_drift.json")
    }
}
