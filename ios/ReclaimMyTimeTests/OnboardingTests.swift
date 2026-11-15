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

    func testConnectOpensServerURLAndHandlesCallback() async throws {
        let h = try T52Harness()
        let calendar = h.model.calendar
        var opened: (URL, String)?
        let connected = await calendar.connect { url, scheme in
            opened = (url, scheme)
            return URL(string: "reclaimmytime://gcal/callback?result=connected")!
        }
        XCTAssertTrue(connected)
        XCTAssertEqual(opened?.0.host(), "accounts.google.com", "opens GET /gcal/connect's authorization_url")
        XCTAssertEqual(opened?.1, "reclaimmytime")
        XCTAssertEqual(calendar.status, .connected)
        XCTAssertNil(calendar.callout)
    }

    func testDeniedAndCancelledOutcomes() async throws {
        let h = try T52Harness()
        let calendar = h.model.calendar
        let denied = await calendar.connect { _, _ in URL(string: "reclaimmytime://gcal/callback?result=error&code=gcal_access_denied")! }
        XCTAssertFalse(denied)
        XCTAssertEqual(calendar.callout, .accessDenied)
        XCTAssertEqual(calendar.status, .notConnected)

        let cancelled = await calendar.connect { _, _ in throw CancellationError() }
        XCTAssertFalse(cancelled)
        XCTAssertNil(calendar.callout, "cancelling the browser sheet is not an error")

        let garbage = await calendar.connect { _, _ in URL(string: "reclaimmytime://elsewhere")! }
        XCTAssertFalse(garbage)
        XCTAssertEqual(calendar.callout, .failed)
    }

    func testNotNowPersistsNotConnectedAndConsentIsPerUser() throws {
        let h = try T52Harness()
        let user = UUID()
        XCTAssertTrue(h.model.calendar.needsConsentStep(userId: user))
        h.model.calendar.skip()
        h.model.calendar.completeConsentStep(userId: user)

        let reloaded = CalendarConnectionModel(api: h.model.api, defaults: h.defaults)
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
