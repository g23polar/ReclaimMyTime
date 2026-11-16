import Foundation
import Observation
import XCTest
@testable import ReclaimMyTime

/// Regression tests for the T5.2 code-review findings (folded into T5.3).
@MainActor
final class T52ReviewFindingsTests: XCTestCase {
    private func gatedModel(_ path: String) throws -> (AppModel, GatedMockTransport, MockTransport, UserDefaults) {
        let mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        let gated = GatedMockTransport(inner: mock, gatedPath: path)
        let suite = "Findings.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        let (store, _) = makeTestSessionStore()
        let model = AppModel(config: AppConfig(backend: .mock), transport: gated, sessionStore: store, defaults: defaults)
        return (model, gated, mock, defaults)
    }

    private func waitForGate(_ gated: GatedMockTransport) async throws {
        for _ in 0..<400 where await gated.pendingCount == 0 {
            try await Task.sleep(for: .milliseconds(5))
        }
        let count = await gated.pendingCount
        XCTAssertEqual(count, 1, "request should be held at the gate")
    }

    // MARK: [major] consent completion must invalidate the root routing decision

    func testCompletingConsentNotifiesObserversOfTheRoutingDecision() throws {
        let h = try T52Harness()
        let user = UUID()
        h.model.calendar.activate(userId: user)
        let changed = LockedFlag()
        withObservationTracking {
            _ = h.model.calendar.needsConsentStep(userId: user)
        } onChange: {
            changed.set()
        }
        h.model.calendar.completeConsentStep(userId: user)
        XCTAssertTrue(changed.value, "RootView must re-evaluate once consent is completed")
        XCTAssertFalse(h.model.calendar.needsConsentStep(userId: user))
    }

    // MARK: [major] calendar identity and async results must not cross accounts

    func testCalendarStateIsClearedOnSignOutAndNotInheritedByTheNextAccount() async throws {
        let h = try T52Harness()
        let userA = UUID(), userB = UUID()
        h.model.handleAuthPhase(.signedIn(StoredSession(token: "a", sessionId: UUID(), userId: userA, expiresAt: .distantFuture)))
        await h.model.calendar.refreshStatus()
        XCTAssertEqual(h.model.calendar.status, .connected)
        XCTAssertNotNil(h.model.calendar.connectedEmail)

        h.model.handleAuthPhase(.signedOut)
        XCTAssertNil(h.model.calendar.connectedEmail, "A's Google email must not survive sign-out")
        XCTAssertEqual(h.model.calendar.status, .unknown)

        h.model.handleAuthPhase(.signedIn(StoredSession(token: "b", sessionId: UUID(), userId: userB, expiresAt: .distantFuture)))
        XCTAssertEqual(h.model.calendar.status, .unknown, "B starts from B's own persisted state, not A's")
        XCTAssertNil(h.model.calendar.connectedEmail)
    }

    func testPersistedCalendarStatusIsScopedPerAccount() async throws {
        let h = try T52Harness()
        let userA = UUID(), userB = UUID()
        h.model.calendar.activate(userId: userA)
        await h.model.calendar.refreshStatus()
        XCTAssertEqual(h.model.calendar.status, .connected)
        let other = CalendarConnectionModel(api: h.model.api, defaults: h.defaults, scope: h.model.calendar.scope)
        other.activate(userId: userB)
        XCTAssertEqual(other.status, .unknown)
        other.activate(userId: userA)
        XCTAssertEqual(other.status, .connected, "A's own status is restored for A")
        let otherBackend = CalendarConnectionModel(api: h.model.api, defaults: h.defaults, scope: "gcal.live.example.com")
        otherBackend.activate(userId: userA)
        XCTAssertEqual(otherBackend.status, .unknown, "status does not cross backends")
    }

    func testLateCalendarStatusFromPreviousAccountIsDropped() async throws {
        let (model, gated, _, _) = try gatedModel("/gcal/status")
        model.handleAuthPhase(.signedIn(StoredSession(token: "a", sessionId: UUID(), userId: UUID(), expiresAt: .distantFuture)))
        let refresh = Task { await model.calendar.refreshStatus() }
        try await waitForGate(gated)
        model.handleAuthPhase(.signedOut)
        model.handleAuthPhase(.signedIn(StoredSession(token: "b", sessionId: UUID(), userId: UUID(), expiresAt: .distantFuture)))
        await gated.releaseAll()
        await refresh.value
        XCTAssertEqual(model.calendar.status, .unknown, "A's delayed status must not land in B's session")
        XCTAssertNil(model.calendar.connectedEmail)
    }

    // MARK: [minor] stale resolution failure must not discard the replacement

    func testStaleConfirmFailureDoesNotReportReplacementAsDiscarded() async throws {
        let a = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)], createdAt: Date(timeIntervalSince1970: 1_000))
        let b = T52Fixtures.staging([(T52Fixtures.portfolioId, T52Fixtures.portfolioDraft)], createdAt: Date(timeIntervalSince1970: 2_000))
        let (model, gated, mock, _) = try gatedModel("/changesets/\(a.id.pathComponent)/confirm")
        await mock.setVariant("not_pending", for: "confirmChangeset")
        var discarded: [(UUID, UUID?)] = []
        model.pending.onDiscarded = { old, replacement in discarded.append((old.id, replacement?.id)) }

        model.pending.stage(a)
        let confirm = Task { await model.pending.confirm(id: a.id) }
        try await waitForGate(gated)
        model.pending.stage(b)
        await gated.releaseAll()
        let confirmed = await confirm.value

        XCTAssertFalse(confirmed)
        XCTAssertEqual(model.pending.pending?.id, b.id, "the replacement stays pending")
        XCTAssertEqual(discarded.count, 1, "only the supersession is reported: \(discarded)")
        XCTAssertEqual(discarded.first?.0, a.id)
        XCTAssertEqual(discarded.first?.1, b.id)
        XCTAssertNil(model.pending.notice, "the user is reviewing B; no out-of-date toast for A")
    }

    // MARK: [minor] older staging responses must stay ignored after the newer one resolves

    func testOlderStagingResponseIsIgnoredAfterNewerResolves() async throws {
        let h = try T52Harness()
        let older = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)], createdAt: Date(timeIntervalSince1970: 1_000))
        let newer = T52Fixtures.staging([(T52Fixtures.portfolioId, T52Fixtures.portfolioDraft)], createdAt: Date(timeIntervalSince1970: 2_000))
        XCTAssertTrue(h.pending.stage(newer))
        await h.pending.reject(id: newer.id)
        XCTAssertNil(h.pending.pending)
        XCTAssertFalse(h.pending.stage(older), "the server expired the older proposal when it created the newer one")
        XCTAssertNil(h.pending.pending)
        XCTAssertFalse(h.pending.stage(newer), "a resolved proposal never becomes pending again")
    }

    // MARK: [major] ignored/superseded preferences proposals must reopen the form

    func testIgnoredPreferencesProposalReopensFormAndStaysOnChat() async throws {
        let h = try T52Harness()
        h.model.navigation.selectedTab = .chat
        h.chat.showPreferencesPrompt()
        let form = try XCTUnwrap(h.chat.preferences)
        form.diversity = .mixed
        // A newer, unrelated proposal (e.g. an agent turn) was adopted while the PUT was in flight.
        let newer = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)], createdAt: Date(timeIntervalSince1970: 4_000_000_000))
        XCTAssertTrue(h.pending.stage(newer))

        let result = await h.chat.submitPreferences()

        XCTAssertNil(result, "the preferences proposal was not adopted")
        XCTAssertEqual(h.pending.pending?.id, newer.id)
        XCTAssertEqual(form.phase, .editing, "the form can be resubmitted")
        XCTAssertEqual(h.model.navigation.selectedTab, .chat, "no jump to Week for a proposal that isn't pending")
    }

    func testSupersededPreferencesProposalReopensUnlessCarriedForward() async throws {
        let h = try T52Harness()
        h.chat.showPreferencesPrompt()
        let form = try XCTUnwrap(h.chat.preferences)
        form.diversity = .mixed
        let submitted = try await XCTUnwrapAsync(await h.chat.submitPreferences())
        XCTAssertEqual(form.phase, .submitted)
        guard case .preferencesUpdated(let prefs) = try XCTUnwrap(submitted.staged.first).change else { return XCTFail("fixture shape") }

        let carried = Changeset(
            id: UUID(), status: .pending, reason: .agent, planId: nil,
            staged: [StagedMutation(source: .agent, interpretation: nil, change: .preferencesUpdated(prefs))],
            createdAt: submitted.createdAt.addingTimeInterval(10), resolvedAt: nil, plan: nil, diff: nil, narration: nil)
        XCTAssertTrue(h.pending.stage(carried))
        XCTAssertEqual(form.phase, .submitted, "the replacement still carries the preferences")

        let unrelated = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)], createdAt: submitted.createdAt.addingTimeInterval(20))
        XCTAssertTrue(h.pending.stage(unrelated))
        XCTAssertEqual(form.phase, .editing, "preferences dropped by the replacement: reopen the form")
    }

    // MARK: [major] partial project cards must not turn editor defaults into changes

    func testPartialCardSendsOnlyUserEditedFields() {
        let unknown = ProjectCard(unknownId: T52Fixtures.workoutId)
        let edit = ProjectEditModel(card: unknown)
        XCTAssertNil(edit.makePatch(), "saving untouched fallbacks sends nothing")
        edit.name = "Workout"
        XCTAssertEqual(edit.makePatch(), ProjectPatch(name: "Workout"), "fallback max block length is not a change")

        var deadline = ProjectCard(unknownId: T52Fixtures.portfolioId)
        deadline = deadline.applying(ProjectPatch(name: "Portfolio site", targetDate: LocalDate("2026-12-04")))
        let partial = ProjectEditModel(card: deadline)
        XCTAssertNil(partial.makePatch(), "unknown estimate fallback is not a change")
        partial.estimatedTotalHours = 12
        XCTAssertEqual(partial.makePatch(), ProjectPatch(estimatedTotalHours: 12))

        let noDate = ProjectCard(unknownId: T52Fixtures.portfolioId).applying(ProjectPatch(estimatedTotalHours: 20))
        let dateless = ProjectEditModel(card: noDate)
        XCTAssertNil(dateless.makePatch(), "unknown date fallback (today) is not a change")
    }

    // MARK: [major] deadline dates are Gregorian regardless of the device calendar

    func testNonGregorianDeviceCalendarKeepsGregorianWireDates() throws {
        var buddhist = Calendar(identifier: .buddhist)
        buddhist.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        var gregorian = Calendar(identifier: .gregorian)
        gregorian.timeZone = buddhist.timeZone
        let card = ProjectCard(id: T52Fixtures.portfolioId, draft: T52Fixtures.portfolioDraft, isNew: false)
        let edit = ProjectEditModel(card: card, calendar: buddhist)

        let expectedStart = try XCTUnwrap(gregorian.date(from: DateComponents(year: 2026, month: 12, day: 12, hour: 12)))
        XCTAssertEqual(edit.targetDate, expectedStart, "the picker opens on the Gregorian 2026-12-12")
        XCTAssertNil(edit.makePatch())

        edit.targetDate = try XCTUnwrap(gregorian.date(from: DateComponents(year: 2026, month: 12, day: 19, hour: 12)))
        XCTAssertEqual(edit.makePatch(), ProjectPatch(targetDate: LocalDate("2026-12-19")), "not Buddhist-era 2569")

        let text = ProjectCardText(locale: Locale(identifier: "en_US"), calendar: buddhist)
        XCTAssertTrue(text.formatDate(LocalDate("2026-12-04")!).hasPrefix("Fri"), "2026-12-04 is a Friday in the Gregorian calendar")
    }
}

/// Thread-safe flag for Observation `onChange` callbacks (which are `@Sendable`).
final class LockedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false
    var value: Bool { lock.withLock { flag } }
    func set() { lock.withLock { flag = true } }
}
