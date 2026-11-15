import SwiftUI
import XCTest
@testable import ReclaimMyTime

// MARK: - shared

enum T53 {
    static let la = TimeZone(identifier: "America/Los_Angeles")!
    static var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = la
        calendar.locale = Locale(identifier: "en_US")
        return calendar
    }
    static var time: TimeText { TimeText(calendar: calendar, locale: Locale(identifier: "en_US")) }

    /// Tue 2026-11-17 10:30 LA: the fixture story's "now".
    static let storyNow = date("2026-11-17T18:30:00Z")

    static func date(_ iso: String) -> Date {
        try! Date(iso, strategy: .iso8601)
    }

    static func span(_ start: String, _ end: String) -> Span {
        Span(start: date(start), end: date(end))!
    }

    static func block(_ span: Span, state: BlockState = .planned, project: UUID = T52Fixtures.portfolioId, gcal: GcalSync = .synced, id: UUID = UUID()) -> Block {
        Block(id: id, planId: UUID(), projectId: project, lineageId: UUID(), span: span, state: state, gcalStatus: gcal, gcalEventId: nil)
    }

    static func pin(_ span: Span, kind: PinKind = .pin, project: UUID? = T52Fixtures.portfolioId) -> BlockPin {
        let json = """
        {"id":"\(UUID().uuidString)","project_id":\(project.map { "\"\($0.uuidString)\"" } ?? "null"),"span":{"start":"\(span.start.ISO8601Format())","end":"\(span.end.ISO8601Format())"},"kind":"\(kind.rawValue)","created_from_block_id":null,"created_at":"2026-11-17T18:00:00Z"}
        """
        return try! JSONCoding.makeDecoder().decode(BlockPin.self, from: Data(json.utf8))
    }

    static func plan() throws -> WeekPlan {
        try JSONCoding.makeDecoder().decode(WeekPlan.self, from: XCTUnwrap(TestFixtures.named("get_plans_current__ok.json").responseBody))
    }

    /// Narrow no-break spaces (en_US "6:00 PM") as plain spaces, for readable expectations.
    static func plain(_ text: String) -> String {
        text.replacingOccurrences(of: "\u{202F}", with: " ").replacingOccurrences(of: "\u{00A0}", with: " ")
    }
}

/// Replies to matching requests with a canned status/body; everything else goes to `inner`.
actor OverrideTransport: HTTPTransport {
    let inner: any HTTPTransport
    private var rules: [(method: String, pathPrefix: String, status: Int, body: Data)] = []
    private(set) var requests: [URLRequest] = []

    init(inner: any HTTPTransport) {
        self.inner = inner
    }

    func override(_ method: String, pathPrefix: String, status: Int, code: String) {
        let body = Data(#"{"error":{"code":"\#(code)","message":"test"}}"#.utf8)
        rules.append((method, pathPrefix, status, body))
    }

    func count(method: String, path: String) -> Int {
        requests.filter { $0.httpMethod == method && $0.url?.path() == path }.count
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        if let rule = rules.first(where: { $0.method == request.httpMethod && (request.url?.path() ?? "").hasPrefix($0.pathPrefix) }),
           let url = request.url,
           let response = HTTPURLResponse(url: url, statusCode: rule.status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"]) {
            return (rule.body, response)
        }
        return try await inner.data(for: request)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        try await inner.stream(for: request)
    }
}

@MainActor
func makeTimetableModel(transport: any HTTPTransport) -> AppModel {
    let suite = "T53.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defaults.removePersistentDomain(forName: suite)
    let (store, _) = makeTestSessionStore()
    let model = AppModel(config: AppConfig(backend: .mock), transport: transport, sessionStore: store, defaults: defaults)
    model.timetable.calendar = T53.calendar
    model.timetable.now = { T53.storyNow }
    return model
}

// MARK: - layers, pin join, layout

final class TimetableLayerTests: XCTestCase {
    private let now = T53.storyNow

    func testClassificationCoversEveryLayer() {
        let future = T53.block(T53.span("2026-11-18T03:00:00Z", "2026-11-18T05:00:00Z"))
        let inProgress = T53.block(T53.span("2026-11-17T18:00:00Z", "2026-11-17T19:00:00Z"))
        let pastPlanned = T53.block(T53.span("2026-11-17T15:00:00Z", "2026-11-17T16:00:00Z"))
        let completed = T53.block(T53.span("2026-11-16T15:00:00Z", "2026-11-16T16:00:00Z"), state: .completed)
        let skipped = T53.block(T53.span("2026-11-16T17:00:00Z", "2026-11-16T18:00:00Z"), state: .skipped)

        let f = BlockClassifier.classify(future, now: now, pinnedIds: [future.id])
        XCTAssertEqual(f.treatment, .planned)
        XCTAssertTrue(f.isPinned)
        XCTAssertFalse(f.isFrozen)
        XCTAssertFalse(f.offersMarkDone || f.offersSkip, "future blocks offer Edit/Delete, not done/skip")

        let p = BlockClassifier.classify(inProgress, now: now, pinnedIds: [])
        XCTAssertEqual(p.treatment, .inProgress)
        XCTAssertTrue(p.isFrozen)
        XCTAssertTrue(p.offersMarkDone && p.offersSkip)

        XCTAssertEqual(BlockClassifier.classify(pastPlanned, now: now, pinnedIds: []).treatment, .pastPlanned)
        XCTAssertTrue(BlockClassifier.classify(pastPlanned, now: now, pinnedIds: []).isFrozen)

        let c = BlockClassifier.classify(completed, now: now, pinnedIds: [])
        XCTAssertEqual(c.treatment, .completed)
        XCTAssertEqual(c.stateWord, "Completed")
        XCTAssertFalse(c.offersMarkDone, "the current state is not re-offered")
        XCTAssertTrue(c.offersSkip)

        let s = BlockClassifier.classify(skipped, now: now, pinnedIds: [])
        XCTAssertEqual(s.treatment, .skipped)
        XCTAssertEqual(s.stateWord, "Skipped")
        XCTAssertTrue(s.offersMarkDone)
        XCTAssertFalse(s.offersSkip)

        // A block starting exactly now is in progress (start ≤ now < end).
        let edge = T53.block(Span(start: now, end: now.addingTimeInterval(3600))!)
        XCTAssertEqual(BlockClassifier.classify(edge, now: now, pinnedIds: []).treatment, .inProgress)
    }

    func testPinJoinNeedsSameProjectAndExactSpan() {
        let span = T53.span("2026-11-20T02:00:00Z", "2026-11-20T04:00:00Z")
        let pinned = T53.block(span)
        let otherProject = T53.block(span, project: T52Fixtures.workoutId)
        let shifted = T53.block(T53.span("2026-11-20T02:05:00Z", "2026-11-20T04:00:00Z"))
        let forbidden = T53.block(T53.span("2026-11-21T02:00:00Z", "2026-11-21T03:00:00Z"))
        let pins = [
            T53.pin(span),
            T53.pin(forbidden.span, kind: .forbid),
            T53.pin(T53.span("2026-11-22T02:00:00Z", "2026-11-22T03:00:00Z"), kind: .forbid, project: nil),
        ]
        let ids = PinJoin.pinnedBlockIds(blocks: [pinned, otherProject, shifted, forbidden], pins: pins)
        XCTAssertEqual(ids, [pinned.id], "project + exact span; forbids never pin")
        XCTAssertEqual(PinJoin.forbids(pins).count, 2)
    }

    func testPinJoinOnFixturesFindsNoPinFromForbidOnlyList() throws {
        let plan = try T53.plan()
        let pins = try JSONCoding.makeDecoder().decode(PinList.self, from: XCTUnwrap(TestFixtures.named("get_pins__ok.json").responseBody)).pins
        XCTAssertTrue(PinJoin.pinnedBlockIds(blocks: plan.blocks, pins: pins).isEmpty)
    }

    func testStagedEditsReadBlockOriginatedPins() throws {
        let edit = StagedEdits.from(try T52Fixtures.changeset("patch_blocks_id__edit_pin.json"))
        let blockId = UUID(uuidString: "077d0c94-4b9f-53e9-8a56-3bf6e5ef377a")!
        XCTAssertEqual(edit[blockId], .moved(to: T53.span("2026-11-20T02:00:00Z", "2026-11-20T04:00:00Z")))
        let delete = StagedEdits.from(try T52Fixtures.changeset("patch_blocks_id__delete_forbid.json"))
        XCTAssertEqual(delete[UUID(uuidString: "7aeff3ac-9ed4-5613-bcf2-08d9060fb725")!], .deleted(T53.span("2026-11-22T23:00:00Z", "2026-11-23T00:00:00Z")))
        XCTAssertTrue(StagedEdits.from(nil).isEmpty)
    }

    func testVoiceOverLabelFormat() {
        let text = BlockText(time: T53.time)
        let block = T53.block(T53.span("2026-11-20T02:00:00Z", "2026-11-20T04:00:00Z"), gcal: .pending)
        let label = text.accessibilityLabel(block, project: "Portfolio site", classification: BlockClassification(treatment: .planned, isPinned: true))
        XCTAssertEqual(T53.plain(label), "Portfolio site, Thursday 6:00 PM to 8:00 PM, 2 hours. Pinned. Planned. Not yet in Google Calendar.")
        let synced = T53.block(T53.span("2026-11-16T15:00:00Z", "2026-11-16T16:30:00Z"), state: .completed)
        XCTAssertEqual(
            T53.plain(text.accessibilityLabel(synced, project: "Workout", classification: BlockClassification(treatment: .completed, isPinned: false))),
            "Workout, Monday 7:00 AM to 8:30 AM, 1 hour 30 minutes. Completed."
        )
    }

    func testTimeRangeFormatting() {
        let time = T53.time
        XCTAssertEqual(T53.plain(time.timeRange(T53.span("2026-11-18T15:00:00Z", "2026-11-18T16:00:00Z"))), "7:00–8:00 AM")
        XCTAssertEqual(T53.plain(time.timeRange(T53.span("2026-11-18T19:00:00Z", "2026-11-18T21:00:00Z"))), "11:00 AM–1:00 PM")
        XCTAssertEqual(T53.plain(time.timeRange(T53.span("2026-11-23T07:00:00Z", "2026-11-23T08:00:00Z"))), "11:00 PM–12:00 AM")
        XCTAssertEqual(TimeText.duration(minutes: 150), "2 h 30 m")
        XCTAssertEqual(time.localDate(LocalDate("2026-11-27")!), "Nov 27")
    }

    func testGridRowsAndOutsideWindowShade() throws {
        let prefs = try JSONCoding.makeDecoder().decode(UserPreferences.self, from: XCTUnwrap(TestFixtures.named("get_preferences__ok.json").responseBody))
        let columns = TimetableGeometry.columns(from: T53.date("2026-11-16T08:00:00Z"), calendar: T53.calendar)
        XCTAssertEqual(columns.count, 7)
        let metrics = TimetableGeometry.metrics(columns: columns, windows: prefs.schedulableWindows, windowTimeZone: T53.la, spans: [], calendar: T53.calendar, pointsPerHour: 44)
        XCTAssertEqual(metrics.startHour, 7)
        XCTAssertEqual(metrics.endHour, 22)
        XCTAssertEqual(metrics.height, 15 * 44)

        let saturday = TimetableGeometry.windowMinutes(for: columns[5], windows: prefs.schedulableWindows, windowTimeZone: T53.la, calendar: T53.calendar)
        let bands = TimetableGeometry.outsideBands(windows: saturday, metrics: metrics)
        XCTAssertEqual(bands.map(\.0), [7 * 60, 20 * 60])
        XCTAssertEqual(bands.map(\.1), [9 * 60, 22 * 60])

        let late = T53.span("2026-11-19T06:00:00Z", "2026-11-19T07:00:00Z") // Wed 10–11 PM
        let widened = TimetableGeometry.metrics(columns: columns, windows: prefs.schedulableWindows, windowTimeZone: T53.la, spans: [late], calendar: T53.calendar, pointsPerHour: 44)
        XCTAssertEqual(widened.endHour, 23, "rows widen so every block stays visible")

        let fallback = TimetableGeometry.metrics(columns: columns, windows: nil, windowTimeZone: nil, spans: [], calendar: T53.calendar, pointsPerHour: 64)
        XCTAssertEqual([fallback.startHour, fallback.endHour], [7, 22])
    }

    func testMidnightCrossingSpanSplitsAcrossColumns() {
        let columns = TimetableGeometry.columns(from: T53.date("2026-11-16T08:00:00Z"), calendar: T53.calendar)
        let span = T53.span("2026-11-17T07:30:00Z", "2026-11-17T08:30:00Z") // Mon 11:30 PM – Tue 12:30 AM
        let segments = TimetableGeometry.segments(span, columns: columns, calendar: T53.calendar)
        XCTAssertEqual(segments, [
            ColumnSegment(column: 0, startMinute: 23 * 60 + 30, endMinute: 24 * 60),
            ColumnSegment(column: 1, startMinute: 0, endMinute: 30),
        ])
    }

    func testAccessibilitySizesUseListLayout() throws {
        XCTAssertEqual(TimetableLayoutMode.mode(for: .large), .grid)
        XCTAssertEqual(TimetableLayoutMode.mode(for: .xxxLarge), .grid)
        XCTAssertEqual(TimetableLayoutMode.mode(for: .accessibility1), .list)
        XCTAssertEqual(TimetableLayoutMode.mode(for: .accessibility5), .list)
        XCTAssertEqual(BlockContent.content(minutes: 120, dynamicTypeSize: .large, compact: false), .nameAndMeta)
        XCTAssertEqual(BlockContent.content(minutes: 120, dynamicTypeSize: .accessibility1, compact: false), .nameOnly, "AX: name only")
        XCTAssertEqual(BlockContent.content(minutes: 30, dynamicTypeSize: .large, compact: false), .nameOnly, "line 2 needs ≥ 45 min")

        let plan = try T53.plan()
        let columns = TimetableGeometry.columns(from: plan.horizon.start, calendar: T53.calendar)
        let sections = DayListLayout.sections(blocks: plan.blocks, columns: columns, time: T53.time)
        XCTAssertEqual(sections.first?.title, "Monday, Nov 16")
        XCTAssertEqual(sections.map(\.day.start), sections.map(\.day.start).sorted(), "days in order")
        XCTAssertEqual(sections.flatMap(\.blocks).count, plan.blocks.count, "every block appears exactly once")
        let first = try XCTUnwrap(sections.first?.blocks.first)
        XCTAssertEqual(T53.plain(BlockText(time: T53.time).listRow(first, project: "Workout")), "Mon 7:00–8:00 AM · Workout · 1 h")
    }
}

// MARK: - drag guards

final class DragGuardTests: XCTestCase {
    private let original = T53.span("2026-11-18T15:00:00Z", "2026-11-18T16:00:00Z") // Wed 7–8 AM
    private let bounds = T53.span("2026-11-18T08:00:00Z", "2026-11-19T08:00:00Z") // Wed (LA)

    func testSnapsToFiveMinutes() {
        XCTAssertEqual(DragGuard.snap(T53.date("2026-11-18T15:02:00Z")), T53.date("2026-11-18T15:00:00Z"))
        XCTAssertEqual(DragGuard.snap(T53.date("2026-11-18T15:03:00Z")), T53.date("2026-11-18T15:05:00Z"))
        let moved = DragGuard.proposedSpan(original: original, handle: .body, deltaMinutes: 73, bounds: bounds)
        XCTAssertEqual(moved, T53.span("2026-11-18T16:15:00Z", "2026-11-18T17:15:00Z"), "start snaps, duration kept")
        let top = DragGuard.proposedSpan(original: original, handle: .top, deltaMinutes: -31, bounds: bounds)
        XCTAssertEqual(top, T53.span("2026-11-18T14:30:00Z", "2026-11-18T16:00:00Z"))
        let bottom = DragGuard.proposedSpan(original: original, handle: .bottom, deltaMinutes: 88, bounds: bounds)
        XCTAssertEqual(bottom, T53.span("2026-11-18T15:00:00Z", "2026-11-18T17:30:00Z"), "a pin may exceed max block length (O6)")
    }

    func testResizeKeepsMinimumAndBodyStaysInBounds() {
        let collapsed = DragGuard.proposedSpan(original: original, handle: .bottom, deltaMinutes: -500, bounds: bounds)
        XCTAssertEqual(TimeText.minutes(collapsed), DragGuard.minimumMinutes)
        let clamped = DragGuard.proposedSpan(original: original, handle: .body, deltaMinutes: 10_000, bounds: bounds)
        XCTAssertEqual(clamped.end, bounds.end, "no cross-day/cross-week drag (D7)")
        XCTAssertEqual(TimeText.minutes(clamped), 60)
    }

    func testPastAndFrozenGuards() {
        let now = T53.date("2026-11-18T15:30:00Z")
        XCTAssertEqual(DragGuard.decide(original: original, proposed: T53.span("2026-11-18T15:20:00Z", "2026-11-18T16:20:00Z"), now: now), .rejectedPast, "any part before now snaps back")
        XCTAssertEqual(DragGuard.decide(original: original, proposed: original, now: T53.storyNow), .unchanged)
        let later = T53.span("2026-11-18T17:00:00Z", "2026-11-18T18:00:00Z")
        XCTAssertEqual(DragGuard.decide(original: original, proposed: later, now: T53.storyNow), .send(later))

        XCTAssertFalse(DragGuard.canPickUp(BlockClassification(treatment: .inProgress, isPinned: false)))
        XCTAssertFalse(DragGuard.canPickUp(BlockClassification(treatment: .pastPlanned, isPinned: false)))
        XCTAssertFalse(DragGuard.canPickUp(BlockClassification(treatment: .completed, isPinned: false)))
        XCTAssertFalse(DragGuard.canPickUp(BlockClassification(treatment: .skipped, isPinned: false)))
        XCTAssertTrue(DragGuard.canPickUp(BlockClassification(treatment: .planned, isPinned: true)))
    }

    func testHapticTickEveryFifteenMinutes() {
        let a = DragGuard.tick(T53.span("2026-11-18T15:00:00Z", "2026-11-18T16:00:00Z"), handle: .body)
        let b = DragGuard.tick(T53.span("2026-11-18T15:10:00Z", "2026-11-18T16:10:00Z"), handle: .body)
        let c = DragGuard.tick(T53.span("2026-11-18T15:15:00Z", "2026-11-18T16:15:00Z"), handle: .body)
        XCTAssertEqual(a, b)
        XCTAssertEqual(c, a + 1)
        XCTAssertNotEqual(DragGuard.tick(original, handle: .bottom), DragGuard.tick(original, handle: .top), "resize ticks follow the moving edge")
    }
}

// MARK: - diff model and banner

final class DiffModelTests: XCTestCase {
    func testEditPinIsOneMovedBlockAgainstLivePlan() throws {
        let diff = try XCTUnwrap(DiffModel(T52Fixtures.changeset("patch_blocks_id__edit_pin.json")))
        XCTAssertFalse(diff.isFirstWeek)
        XCTAssertTrue(diff.showsNewBadges)
        XCTAssertEqual(diff.title, "Review changes")
        XCTAssertEqual(diff.count(.moved), 1)
        XCTAssertEqual(diff.count(.unchanged), 8)
        let moved = try XCTUnwrap(diff.changes.first)
        XCTAssertEqual(moved.lineageId, UUID(uuidString: "1a2b3c4d-5e6f-4a7b-8c9d-000000000003"))
        XCTAssertFalse(moved.isResizeOnly, "start and end both changed: a move with a connector")
        XCTAssertEqual(moved.block?.lineageId, moved.lineageId, "keyed by lineage_id onto the proposed plan's block")
        XCTAssertEqual(
            T53.plain(DiffText(time: T53.time).accessibilityLabel(moved, project: "Portfolio site", firstWeek: false)),
            "Moved: Portfolio site, from Thursday 7:00–9:00 PM to Thursday 6:00–8:00 PM"
        )
    }

    func testAgentChangesetMovedAddedRemoved() throws {
        let changeset = try XCTUnwrap(T52Fixtures.agentDone("post_agent_message__laptop.json").changeset)
        let diff = try XCTUnwrap(DiffModel(changeset))
        XCTAssertEqual([diff.count(.moved), diff.count(.added), diff.count(.removed)], [2, 1, 1])
        let removed = try XCTUnwrap(diff.items.first { $0.kind == .removed })
        XCTAssertNil(removed.block)
        XCTAssertNil(removed.after)
        XCTAssertEqual(removed.displaySpan, removed.before)
        let columns = TimetableGeometry.columns(from: diff.plan.horizon.start, calendar: T53.calendar)
        let grouped = diff.changesByDay(columns: columns)
        XCTAssertEqual(grouped.flatMap(\.items).count, 4, "the list shows changes only")
        XCTAssertTrue(grouped.allSatisfy { group in group.items.allSatisfy { $0.kind != .unchanged } })
    }

    func testResizeOnlyMovesAreLabelledWithNewLength() throws {
        let before = T53.span("2026-11-20T03:00:00Z", "2026-11-20T05:00:00Z")
        let after = T53.span("2026-11-20T03:00:00Z", "2026-11-20T05:30:00Z")
        let item = DiffItem(lineageId: UUID(), projectId: UUID(), kind: .moved, before: before, after: after, block: nil)
        XCTAssertTrue(item.isResizeOnly)
        XCTAssertEqual(DiffText(time: T53.time).resizeLabel(item), "Now 2 h 30 m")
        let topResize = DiffItem(lineageId: UUID(), projectId: UUID(), kind: .moved, before: before, after: T53.span("2026-11-20T02:30:00Z", "2026-11-20T05:00:00Z"), block: nil)
        XCTAssertTrue(topResize.isResizeOnly)
        let move = DiffItem(lineageId: UUID(), projectId: UUID(), kind: .moved, before: before, after: T53.span("2026-11-20T04:00:00Z", "2026-11-20T06:00:00Z"), block: nil)
        XCTAssertFalse(move.isResizeOnly)
        XCTAssertNil(DiffText(time: T53.time).resizeLabel(move))
    }

    func testFirstWeekModeSuppressesNewBadges() throws {
        let diff = try XCTUnwrap(DiffModel(T52Fixtures.changeset("put_preferences__first_week.json")))
        XCTAssertTrue(diff.isFirstWeek)
        XCTAssertFalse(diff.showsNewBadges)
        XCTAssertEqual(diff.title, "Your first week")
        XCTAssertEqual(diff.count(.added), 9)
        let item = try XCTUnwrap(diff.items.first)
        XCTAssertFalse(DiffText(time: T53.time).accessibilityLabel(item, project: "Workout", firstWeek: true).hasPrefix("New:"))
        XCTAssertTrue(DiffText(time: T53.time).accessibilityLabel(item, project: "Workout", firstWeek: false).hasPrefix("New:"))
    }

    func testPlanlessChangesetHasNoDiff() throws {
        XCTAssertNil(DiffModel(try T52Fixtures.changeset("post_projects__perennial_onboarding.json")))
    }

    func testBannerCountTextOmitsZeros() throws {
        let laptop = try XCTUnwrap(T52Fixtures.agentDone("post_agent_message__laptop.json").changeset)
        XCTAssertEqual(ProposalSummary.make(laptop).summary, "2 blocks moved · 1 added · 1 removed")
        XCTAssertEqual(ProposalSummary.make(try T52Fixtures.changeset("patch_blocks_id__edit_pin.json")).summary, "1 block moved")
        XCTAssertEqual(ProposalSummary.make(try T52Fixtures.changeset("patch_projects_id__weekly_target.json")).summary, "1 removed")
        XCTAssertEqual(ProposalSummary.make(try T52Fixtures.changeset("patch_blocks_id__delete_forbid.json")).summary, "1 added · 1 removed")
        let infeasible = ProposalSummary.make(try T52Fixtures.changeset("post_projects__deadline_infeasible.json"))
        XCTAssertEqual(infeasible.summary, "4 added")
        XCTAssertTrue(infeasible.doesNotFit)
        XCTAssertEqual(ProposalReviewCopy.confirmTitle(try T52Fixtures.changeset("post_projects__deadline_infeasible.json")), "Confirm what fits")
        XCTAssertEqual(ProposalReviewCopy.confirmTitle(laptop), "Confirm changes")
        XCTAssertEqual(ProposalReviewCopy.rejectTitle, "Keep my week")
    }
}
