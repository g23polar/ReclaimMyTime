import Foundation
import os
import SwiftUI
import XCTest
@testable import ReclaimMyTime

// MARK: - fakes

/// In-memory `UNUserNotificationCenter`: pending requests keyed by identifier (add replaces).
actor FakeNotificationCenter: LocalNotificationCenter {
    var status: NotificationAuthorization
    var grantsOnRequest: Bool
    private(set) var pending: [String: LocalNotificationRequest] = [:]
    private(set) var delivered: [LocalNotificationRequest] = []
    private(set) var authorizationRequests = 0
    private(set) var categoriesRegistered = false

    init(status: NotificationAuthorization = .authorized, grantsOnRequest: Bool = true) {
        self.status = status
        self.grantsOnRequest = grantsOnRequest
    }

    func setStatus(_ value: NotificationAuthorization) { status = value }

    func authorization() async -> NotificationAuthorization { status }

    func requestAuthorization() async -> Bool {
        authorizationRequests += 1
        if status == .notDetermined { status = grantsOnRequest ? .authorized : .denied }
        return status == .authorized
    }

    func registerCategories() async { categoriesRegistered = true }

    func pendingIdentifiers() async -> [String] { Array(pending.keys) }

    func add(_ request: LocalNotificationRequest) async throws {
        if request.fireDate == nil {
            delivered.append(request)
        } else {
            pending[request.identifier] = request
        }
    }

    func removePending(identifiers: [String]) async {
        for id in identifiers { pending[id] = nil }
    }

    var sortedPendingIds: [String] { pending.keys.sorted() }
}

final class FakeBackgroundScheduler: BackgroundRefreshScheduling, @unchecked Sendable {
    private let lock = NSLock()
    private var _submissions: [(String, Date)] = []
    var submissions: [(String, Date)] { lock.withLock { _submissions } }

    func submitAppRefresh(identifier: String, earliestBeginDate: Date) throws {
        lock.withLock { _submissions.append((identifier, earliestBeginDate)) }
    }
}

struct FakeDriftSource: DriftSource {
    let alert: DriftAlert?
    func pendingDrift(livePlan: WeekPlan) async throws -> DriftAlert? { alert }
}

// MARK: - builders

enum T54 {
    static let reading = UUID(uuidString: "00000000-0000-4000-8000-0000000000a1")!
    static let grant = UUID(uuidString: "00000000-0000-4000-8000-0000000000a2")!
    static let workout = UUID(uuidString: "00000000-0000-4000-8000-0000000000a3")!
    static let portfolio = UUID(uuidString: "00000000-0000-4000-8000-0000000000a4")!

    static func project(_ id: UUID, name: String, deadline: String? = nil) -> Project {
        let goal = deadline.map { #""kind":"deadline","target_date":"\#($0)","estimated_total_hours":10.0,"weekly_target_hours":null"# }
            ?? #""kind":"perennial","target_date":null,"estimated_total_hours":null,"weekly_target_hours":3.0"#
        let json = """
        {"id":"\(id.uuidString)","name":"\(name)",\(goal),"max_block_minutes":120,"min_block_minutes":30,"preferred_windows":null,"preferred_strict":false,"requirements":[],"status":"active","created_at":"2026-11-15T00:00:00Z"}
        """
        return try! JSONCoding.makeDecoder().decode(Project.self, from: Data(json.utf8))
    }

    static func block(_ start: String, minutes: Int, project: UUID, state: BlockState = .planned) -> Block {
        let begin = T53.date(start)
        return Block(id: UUID(), planId: UUID(), projectId: project, lineageId: UUID(), span: Span(start: begin, end: begin.addingTimeInterval(Double(minutes) * 60))!, state: state, gcalStatus: .synced, gcalEventId: nil)
    }

    static func plan(_ blocks: [Block], timezone: String = "America/Los_Angeles") -> WeekPlan {
        WeekPlan(
            id: UUID(), horizon: T53.span("2026-11-16T08:00:00Z", "2026-11-23T08:00:00Z"), weekStart: LocalDate("2026-11-16")!,
            timezone: timezone, version: 1, status: .live, infeasibility: nil, createdAt: T53.date("2026-11-15T00:00:00Z"),
            supersededAt: nil, blocks: blocks.sorted { $0.span.start < $1.span.start }
        )
    }

    static func snapshot(_ plan: WeekPlan, projects: [Project], pins: [BlockPin] = []) -> NotificationSnapshot {
        let byId = Dictionary(uniqueKeysWithValues: projects.map { ($0.id, $0) })
        return NotificationSnapshot(plan: plan, pins: pins, projects: byId, projectNames: byId.mapValues(\.name))
    }

    static func planner(now: Date = T53.storyNow, warnings: Bool = false) -> BlockNotificationPlanner {
        BlockNotificationPlanner(now: now, warningsEnabled: warnings, calendar: T53.calendar, locale: Locale(identifier: "en_US"))
    }

    @MainActor
    static func scheduler(center: FakeNotificationCenter, defaults: UserDefaults = freshDefaults()) -> NotificationScheduler {
        let scheduler = NotificationScheduler(center: center, defaults: defaults)
        scheduler.now = { T53.storyNow }
        scheduler.calendar = T53.calendar
        return scheduler
    }

    static func freshDefaults() -> UserDefaults {
        let suite = "T54.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    /// A signed-in AppModel on the fixture mock with fakes for every system service.
    @MainActor
    static func model(
        transport: any HTTPTransport,
        center: FakeNotificationCenter,
        background: FakeBackgroundScheduler = FakeBackgroundScheduler(),
        drift: (any DriftSource)? = nil,
        defaults: UserDefaults = freshDefaults()
    ) async -> AppModel {
        let (store, _) = makeTestSessionStore()
        let model = AppModel(config: AppConfig(backend: .mock), transport: transport, sessionStore: store, defaults: defaults, notificationCenter: center, backgroundScheduler: background, driftSource: drift)
        model.timetable.calendar = T53.calendar
        model.timetable.now = { T53.storyNow }
        model.notifications.now = { T53.storyNow }
        model.notifications.calendar = T53.calendar
        model.timezone.deviceTimeZone = { T53.la }
        await model.auth.signInWithApple(identityToken: "mock-identity-token", authorizationCode: "mock-authorization-code", rawNonce: Nonce.random())
        return model
    }

    static func lineage(_ suffix: String) -> UUID {
        UUID(uuidString: "1a2b3c4d-5e6f-4a7b-8c9d-0000000000\(suffix)")!
    }
}

// MARK: - window, cap, copy (pure)

final class BlockNotificationPlannerTests: XCTestCase {
    func testWindowHoldsPlannedBlocksStartingInTheNext48Hours() throws {
        let plan = try T53.plan()
        let requests = T54.planner().requests(for: NotificationSnapshot(plan: plan))
        // Tue 10:30 LA: lineages 01–02 are past; 03, 04, 05 start before Thu 10:30; 06 is after.
        XCTAssertEqual(requests.map(\.identifier), ["03", "04", "05"].map { BlockNotificationPlanner.startIdentifier(lineage: T54.lineage($0)) })
        XCTAssertEqual(requests.map(\.fireDate), ["03", "04", "05"].compactMap { suffix in plan.blocks.first { $0.lineageId == T54.lineage(suffix) }?.span.start })
        XCTAssertTrue(requests.allSatisfy { $0.categoryIdentifier == NotificationCategoryID.blockStart })
    }

    func testSkippedAndCompletedBlocksAreNotNotified() {
        let plan = T54.plan([
            T54.block("2026-11-17T20:00:00Z", minutes: 60, project: T54.reading, state: .skipped),
            T54.block("2026-11-17T22:00:00Z", minutes: 60, project: T54.reading),
        ])
        XCTAssertEqual(T54.planner().requests(for: NotificationSnapshot(plan: plan)).count, 1)
    }

    func testCapIs64WithBlockStartsBeforeWarnings() {
        let start = T53.storyNow.addingTimeInterval(3600)
        let many = (0..<70).map { i in
            T54.block(start.addingTimeInterval(Double(i) * 1800).ISO8601Format(), minutes: 25, project: T54.reading)
        }
        let full = T54.planner(warnings: true).requests(for: NotificationSnapshot(plan: T54.plan(many)))
        XCTAssertEqual(full.count, BlockNotificationPlanner.pendingCap)
        XCTAssertTrue(full.allSatisfy { $0.categoryIdentifier == NotificationCategoryID.blockStart }, "starts take every slot first")
        XCTAssertEqual(full.last?.fireDate, many[63].span.start, "the earliest 64 starts are kept")

        let forty = Array(many.prefix(40))
        let mixed = T54.planner(warnings: true).requests(for: NotificationSnapshot(plan: T54.plan(forty)))
        XCTAssertEqual(mixed.count, 64)
        XCTAssertEqual(mixed.filter { $0.categoryIdentifier == NotificationCategoryID.blockStart }.count, 40)
        let warnings = mixed.filter { $0.categoryIdentifier == NotificationCategoryID.blockSoon }
        XCTAssertEqual(warnings.count, 24, "warnings fill only the room left")
        XCTAssertEqual(warnings.map(\.fireDate), forty.prefix(24).map { $0.span.start.addingTimeInterval(-600) })
    }

    func testWarningSkippedWhenItsTimeHasPassed() {
        let soon = T54.block(T53.storyNow.addingTimeInterval(5 * 60).ISO8601Format(), minutes: 60, project: T54.reading)
        let requests = T54.planner(warnings: true).requests(for: NotificationSnapshot(plan: T54.plan([soon])))
        XCTAssertEqual(requests.map(\.categoryIdentifier), [NotificationCategoryID.blockStart])
    }

    // §1 copy, one test per row plus first-match precedence.

    private func body(_ target: Block, in blocks: [Block], projects: [Project], pins: [BlockPin] = []) throws -> (String, String) {
        let requests = T54.planner().requests(for: T54.snapshot(T54.plan(blocks), projects: projects, pins: pins))
        let request = try XCTUnwrap(requests.first { $0.identifier == BlockNotificationPlanner.startIdentifier(lineage: target.lineageId) })
        return (request.title, request.body)
    }

    func testDefaultRow() throws {
        let first = T54.block("2026-11-17T20:00:00Z", minutes: 60, project: T54.reading)  // Tue 12:00
        let later = T54.block("2026-11-18T23:00:00Z", minutes: 60, project: T54.reading)  // Wed 15:00
        let (title, body) = try body(first, in: [first, later], projects: [T54.project(T54.reading, name: "Reading")])
        XCTAssertEqual(title, "Reading")
        XCTAssertEqual(body, "Next 1 hour: Reading. That's all the time it gets today.")
    }

    func testLastBlockOfTheWeekRow() throws {
        let earlier = T54.block("2026-11-17T20:00:00Z", minutes: 60, project: T54.reading)
        let last = T54.block("2026-11-18T23:00:00Z", minutes: 90, project: T54.reading)
        let (_, body) = try body(last, in: [earlier, last], projects: [T54.project(T54.reading, name: "Reading")])
        XCTAssertEqual(body, "Last Reading session this week: 90 minutes.")
    }

    func testDeadlineWithinThreeDaysRow() throws {
        let block = T54.block("2026-11-17T20:00:00Z", minutes: 120, project: T54.grant)  // Tue 17 Nov LA
        let after = T54.block("2026-11-18T20:00:00Z", minutes: 60, project: T54.grant)
        let (_, body) = try body(block, in: [block, after], projects: [T54.project(T54.grant, name: "Grant", deadline: "2026-11-19")])
        XCTAssertEqual(body, "2 hours on Grant. 2 days to go.")
    }

    func testDeadlineFurtherOutFallsThrough() throws {
        let block = T54.block("2026-11-17T20:00:00Z", minutes: 120, project: T54.grant)
        let after = T54.block("2026-11-18T20:00:00Z", minutes: 60, project: T54.grant)
        let (_, body) = try body(block, in: [block, after], projects: [T54.project(T54.grant, name: "Grant", deadline: "2026-11-21")])
        XCTAssertEqual(body, "Next 2 hours: Grant. That's all the time it gets today.")
    }

    func testPinnedRow() throws {
        let block = T54.block("2026-11-17T20:00:00Z", minutes: 90, project: T54.portfolio)
        let after = T54.block("2026-11-18T20:30:00Z", minutes: 60, project: T54.portfolio)
        let (_, body) = try body(block, in: [block, after], projects: [T54.project(T54.portfolio, name: "Portfolio")], pins: [T53.pin(block.span, project: T54.portfolio)])
        XCTAssertEqual(body, "Your 90 minutes for Portfolio, as you set it.")
    }

    func testPerennialSameTimeAsUsualRow() throws {
        let tuesday = T54.block("2026-11-17T22:00:00Z", minutes: 60, project: T54.workout)  // 14:00 LA
        let thursday = T54.block("2026-11-19T22:00:00Z", minutes: 60, project: T54.workout)  // 14:00 LA
        let (_, body) = try body(tuesday, in: [tuesday, thursday], projects: [T54.project(T54.workout, name: "Workout")])
        XCTAssertEqual(body, "1 hour of Workout, same as always.")
    }

    func testFirstMatchingRowWins() throws {
        // Last of the week beats pinned.
        let earlier = T54.block("2026-11-17T20:00:00Z", minutes: 60, project: T54.portfolio)
        let lastPinned = T54.block("2026-11-18T20:00:00Z", minutes: 60, project: T54.portfolio)
        XCTAssertEqual(try body(lastPinned, in: [earlier, lastPinned], projects: [T54.project(T54.portfolio, name: "Portfolio")], pins: [T53.pin(lastPinned.span, project: T54.portfolio)]).1,
                       "Last Portfolio session this week: 1 hour.")
        // Deadline beats pinned.
        let pinnedDeadline = T54.block("2026-11-17T20:00:00Z", minutes: 60, project: T54.grant)
        let after = T54.block("2026-11-18T20:00:00Z", minutes: 60, project: T54.grant)
        XCTAssertEqual(try body(pinnedDeadline, in: [pinnedDeadline, after], projects: [T54.project(T54.grant, name: "Grant", deadline: "2026-11-18")], pins: [T53.pin(pinnedDeadline.span, project: T54.grant)]).1,
                       "1 hour on Grant. 1 day to go.")
    }

    func testDurationFormat() {
        XCTAssertEqual(NotificationCopy.duration(minutes: 60), "1 hour")
        XCTAssertEqual(NotificationCopy.duration(minutes: 120), "2 hours")
        XCTAssertEqual(NotificationCopy.duration(minutes: 90), "90 minutes")
        XCTAssertEqual(NotificationCopy.duration(minutes: 45), "45 minutes")
        XCTAssertEqual(NotificationCopy.duration(minutes: 150), "2 hours 30 minutes")
        XCTAssertEqual(NotificationCopy.duration(minutes: 121), "2 hours 1 minute")
        XCTAssertEqual(NotificationCopy.daysToGo(0), "Due today.")
        XCTAssertEqual(NotificationCopy.daysToGo(3), "3 days to go.")
    }

    func testWarningCopy() throws {
        let block = T54.block("2026-11-17T20:00:00Z", minutes: 60, project: T54.reading)  // 12:00 PM LA
        let requests = T54.planner(warnings: true).requests(for: T54.snapshot(T54.plan([block]), projects: [T54.project(T54.reading, name: "Reading")]))
        let warning = try XCTUnwrap(requests.first { $0.categoryIdentifier == NotificationCategoryID.blockSoon })
        XCTAssertEqual(warning.identifier, BlockNotificationPlanner.soonIdentifier(lineage: block.lineageId))
        XCTAssertEqual(warning.title, "In 10 minutes: Reading")
        XCTAssertEqual(T53.plain(warning.body), "1 hour starting at 12:00 PM. Wrap up what you're doing.")
        XCTAssertEqual(warning.fireDate, block.span.start.addingTimeInterval(-600))
    }

    func testDriftCopy() {
        let alert = DriftAlert(changesetId: UUID(), projectId: T54.reading, blockStart: T53.date("2026-11-17T20:00:00Z"))
        let request = DriftNotification.request(alert, projectName: "Reading", time: T53.time)
        XCTAssertEqual(request.title, "A meeting landed on your plan")
        XCTAssertEqual(T53.plain(request.body), "Reading at 12:00 PM clashes with something new in your calendar. Tap to see a fix.")
        XCTAssertNil(request.fireDate)
    }

    // Responses → routes.

    func testRoutesFromNotificationResponses() {
        let lineage = UUID(), block = UUID()
        let start = [NotificationRoute.Key.kind: "block_start", NotificationRoute.Key.lineageId: lineage.uuidString, NotificationRoute.Key.blockId: block.uuidString]
        let soon = [NotificationRoute.Key.kind: "block_soon", NotificationRoute.Key.lineageId: lineage.uuidString]
        func route(_ action: String, _ info: [String: String]) -> NotificationRoute? {
            NotificationRoute(actionIdentifier: action, userInfo: info, defaultAction: "default", dismissAction: "dismiss")
        }
        XCTAssertEqual(route(NotificationActionID.skip, start), .skipBlock(lineageId: lineage, blockId: block))
        XCTAssertEqual(route(NotificationActionID.open, start), .openBlock(lineageId: lineage, blockId: block))
        XCTAssertEqual(route("default", soon), .openBlock(lineageId: lineage, blockId: nil))
        XCTAssertNil(route(NotificationActionID.skip, soon), "BLOCK_SOON has no Skip")
        XCTAssertNil(route("dismiss", start))
        XCTAssertEqual(route("default", [NotificationRoute.Key.kind: "drift"]), .drift(changesetId: nil))
    }
}

// MARK: - scheduler

@MainActor
final class NotificationSchedulerTests: XCTestCase {
    func testRebuildReplacesRatherThanDuplicates() async throws {
        let center = FakeNotificationCenter()
        let scheduler = T54.scheduler(center: center)
        let plan = try T53.plan()
        await scheduler.rebuild(NotificationSnapshot(plan: plan))
        let first = await center.sortedPendingIds
        await scheduler.rebuild(NotificationSnapshot(plan: plan))
        let second = await center.sortedPendingIds
        XCTAssertEqual(first.count, 3)
        XCTAssertEqual(first, second, "identifiers are keyed by lineage and kind")

        // A re-plan that drops lineage 04 and moves 05 keeps 05's identifier and removes 04's.
        let moved = plan.blocks.compactMap { block -> Block? in
            if block.lineageId == T54.lineage("04") { return nil }
            guard block.lineageId == T54.lineage("05") else { return block }
            return Block(id: UUID(), planId: block.planId, projectId: block.projectId, lineageId: block.lineageId, span: Span(start: block.span.start.addingTimeInterval(3600), end: block.span.end.addingTimeInterval(3600))!, state: .planned, gcalStatus: .pending, gcalEventId: nil)
        }
        let replanned = T54.plan(moved)
        await scheduler.rebuild(NotificationSnapshot(plan: replanned))
        let third = await center.sortedPendingIds
        XCTAssertEqual(third, ["03", "05"].map { BlockNotificationPlanner.startIdentifier(lineage: T54.lineage($0)) }.sorted())
        let fireDate = await center.pending[BlockNotificationPlanner.startIdentifier(lineage: T54.lineage("05"))]?.fireDate
        XCTAssertEqual(fireDate, moved.first { $0.lineageId == T54.lineage("05") }?.span.start)
    }

    func testRebuildLeavesOtherNotificationsAlone() async throws {
        let center = FakeNotificationCenter()
        try await center.add(LocalNotificationRequest(identifier: "other", title: "", body: "", categoryIdentifier: "", fireDate: T53.storyNow.addingTimeInterval(60), userInfo: [:]))
        let scheduler = T54.scheduler(center: center)
        await scheduler.rebuild(NotificationSnapshot(plan: try T53.plan()))
        await scheduler.rebuild(.empty)
        let ids = await center.sortedPendingIds
        XCTAssertEqual(ids, ["other"])
    }

    func testNothingIsScheduledWithoutPermission() async throws {
        let center = FakeNotificationCenter(status: .denied)
        let scheduler = T54.scheduler(center: center)
        await scheduler.rebuild(NotificationSnapshot(plan: try T53.plan()))
        let ids = await center.sortedPendingIds
        XCTAssertTrue(ids.isEmpty)
    }

    func testWarningToggleAddsAndRemovesWarnings() async throws {
        let center = FakeNotificationCenter()
        let defaults = T54.freshDefaults()
        let scheduler = T54.scheduler(center: center, defaults: defaults)
        XCTAssertFalse(scheduler.warningsEnabled, "off by default")
        await scheduler.rebuild(NotificationSnapshot(plan: try T53.plan()))
        var ids = await center.sortedPendingIds
        XCTAssertFalse(ids.contains { $0.hasPrefix(BlockNotificationPlanner.soonPrefix) })

        scheduler.warningsEnabled = true
        await scheduler.waitForIdle()
        ids = await center.sortedPendingIds
        XCTAssertEqual(ids.filter { $0.hasPrefix(BlockNotificationPlanner.soonPrefix) }.count, 3)
        XCTAssertTrue(defaults.bool(forKey: NotificationScheduler.warningsKey), "persisted")
        XCTAssertTrue(T54.scheduler(center: center, defaults: defaults).warningsEnabled)

        scheduler.warningsEnabled = false
        await scheduler.waitForIdle()
        ids = await center.sortedPendingIds
        XCTAssertEqual(ids.count, 3)
        XCTAssertFalse(ids.contains { $0.hasPrefix(BlockNotificationPlanner.soonPrefix) })
    }
}

// MARK: - app integration

@MainActor
final class NotificationAppTests: XCTestCase {
    private var gate: GatedTransport!

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
    }

    func testPlanChangeRebuildsTheWindow() async throws {
        let center = FakeNotificationCenter()
        let model = await T54.model(transport: gate, center: center)
        await model.timetable.load()
        await model.notifications.waitForIdle()
        var ids = await center.sortedPendingIds
        XCTAssertEqual(ids.count, 3)
        let body = await center.pending[BlockNotificationPlanner.startIdentifier(lineage: T54.lineage("03"))]?.body
        XCTAssertEqual(body, "Next 2 hours: Portfolio site. That's all the time it gets today.", "copy uses the loaded project names")

        // A newer live plan (here: lineage 03 skipped elsewhere) re-registers without it.
        await gate.respond("GET", "/plans/current", body: try T53Bodies.plan { block in
            var block = block
            if (block["lineage_id"] as? String)?.hasSuffix("03") == true { block["state"] = "skipped" }
            return block
        })
        await model.timetable.load()
        await model.notifications.waitForIdle()
        ids = await center.sortedPendingIds
        XCTAssertEqual(ids, ["04", "05"].map { BlockNotificationPlanner.startIdentifier(lineage: T54.lineage($0)) }.sorted())
    }

    func testForegroundRebuilds() async throws {
        let center = FakeNotificationCenter()
        let model = await T54.model(transport: gate, center: center)
        await model.handleForeground()
        let ids = await center.sortedPendingIds
        XCTAssertEqual(ids.count, 3)
    }

    func testSignOutClearsTheWindow() async throws {
        let center = FakeNotificationCenter()
        let model = await T54.model(transport: gate, center: center)
        await model.timetable.load()
        await model.notifications.waitForIdle()
        model.resetSessionState()
        await model.notifications.waitForIdle()
        let ids = await center.sortedPendingIds
        XCTAssertTrue(ids.isEmpty)
    }

    func testSkipActionSendsPatchSkip() async throws {
        let model = await T54.model(transport: gate, center: FakeNotificationCenter())
        await model.timetable.load()
        let block = try XCTUnwrap(model.timetable.plan?.blocks.first { $0.lineageId == T54.lineage("03") })
        await model.handle(.skipBlock(lineageId: block.lineageId, blockId: block.id))
        let patches = await gate.requests.filter { $0.httpMethod == "PATCH" && $0.url?.path() == "/blocks/\(block.id.pathComponent)" }
        XCTAssertEqual(patches.count, 1)
        XCTAssertEqual(try JSONCoding.makeDecoder().decode(BlockAction.self, from: XCTUnwrap(patches.first?.httpBody)), .skip)
    }

    func testSkipFromAColdStartLoadsThePlanAndUsesTheCurrentBlockForTheLineage() async throws {
        let model = await T54.model(transport: gate, center: FakeNotificationCenter())
        let current = try XCTUnwrap(try T53.plan().blocks.first { $0.lineageId == T54.lineage("04") })
        await model.handle(.skipBlock(lineageId: current.lineageId, blockId: UUID()))
        let patched = await gate.requests.filter { $0.httpMethod == "PATCH" }.map { $0.url?.path() }
        XCTAssertEqual(patched, ["/blocks/\(current.id.pathComponent)"], "the lineage resolves to the live block, not a stale id")
    }

    func testOpenActionShowsTheBlocksDay() async throws {
        let model = await T54.model(transport: gate, center: FakeNotificationCenter())
        model.navigation.selectedTab = .settings
        await model.handle(.openBlock(lineageId: T54.lineage("04"), blockId: nil))
        XCTAssertEqual(model.navigation.selectedTab, .week)
        XCTAssertEqual(model.timetable.requestedDay, T53.calendar.startOfDay(for: T53.date("2026-11-18T15:00:00Z")))
    }

    // Permission pre-prompt.

    private func confirmFirstWeek(_ model: AppModel) async throws {
        let changeset = try T52Fixtures.changeset("put_preferences__first_week.json")
        XCTAssertTrue(model.pending.stage(changeset))
        let confirmed = await model.pending.confirm(id: changeset.id)
        XCTAssertTrue(confirmed)
    }

    func testPrePromptAppearsOnlyAfterTheFirstWeekIsConfirmed() async throws {
        let center = FakeNotificationCenter(status: .notDetermined)
        let model = await T54.model(transport: gate, center: center)
        await model.handleForeground()
        await model.timetable.load()
        XCTAssertFalse(model.notifications.showsPrePrompt, "never at launch")
        let asked = await center.authorizationRequests
        XCTAssertEqual(asked, 0, "the system prompt is never shown unasked")

        try await confirmFirstWeek(model)
        await waitUntil { model.notifications.showsPrePrompt }
    }

    func testOnboardingConfirmWithoutAPlanDoesNotPrompt() async throws {
        let center = FakeNotificationCenter(status: .notDetermined)
        let model = await T54.model(transport: gate, center: center)
        let changeset = try T52Fixtures.changeset("post_projects__perennial_onboarding.json")
        XCTAssertNil(changeset.planId)
        model.pending.onConfirmed?(changeset)
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertFalse(model.notifications.showsPrePrompt, "no week yet, so no nudge to offer")
    }

    func testNotNowIsRemembered() async throws {
        let center = FakeNotificationCenter(status: .notDetermined)
        let defaults = T54.freshDefaults()
        let model = await T54.model(transport: gate, center: center, defaults: defaults)
        try await confirmFirstWeek(model)
        await waitUntil { model.notifications.showsPrePrompt }
        model.notifications.declinePrePrompt()
        XCTAssertFalse(model.notifications.showsPrePrompt)

        let relaunched = await T54.model(transport: gate, center: center, defaults: defaults)
        await relaunched.notifications.weekConfirmed()
        XCTAssertFalse(relaunched.notifications.showsPrePrompt, "Not now persists across launches")
        let asked = await center.authorizationRequests
        XCTAssertEqual(asked, 0)
    }

    func testTurnOnAsksTheSystemAndFillsTheWindow() async throws {
        let center = FakeNotificationCenter(status: .notDetermined)
        let model = await T54.model(transport: gate, center: center)
        await model.timetable.load()
        await model.notifications.waitForIdle()
        var ids = await center.sortedPendingIds
        XCTAssertTrue(ids.isEmpty)
        try await confirmFirstWeek(model)
        await waitUntil { model.notifications.showsPrePrompt }
        await model.notifications.acceptPrePrompt()
        let asked = await center.authorizationRequests
        XCTAssertEqual(asked, 1)
        XCTAssertFalse(model.notifications.showsPrePrompt)
        ids = await center.sortedPendingIds
        XCTAssertFalse(ids.isEmpty)
    }

    // Background refresh.

    func testEnteringBackgroundSchedulesTheDriftCheck() async throws {
        let background = FakeBackgroundScheduler()
        let model = await T54.model(transport: gate, center: FakeNotificationCenter(), background: background)
        model.handleScenePhase(.background)
        XCTAssertEqual(background.submissions.map(\.0), [BackgroundTaskID.driftCheck])
        XCTAssertEqual(background.submissions.first?.1, T53.storyNow.addingTimeInterval(BackgroundTaskID.refreshInterval))
        XCTAssertEqual(BackgroundTaskID.driftCheck, "com.reclaimmytime.app.drift-check")
    }

    func testSignedOutDoesNotScheduleBackgroundWork() async throws {
        let background = FakeBackgroundScheduler()
        let model = await T54.model(transport: gate, center: FakeNotificationCenter(), background: background)
        await model.auth.signOut()
        model.handleScenePhase(.background)
        XCTAssertTrue(background.submissions.isEmpty)
    }

    func testBackgroundRefreshFetchesThePlanReschedulesAndStubNoOps() async throws {
        let center = FakeNotificationCenter()
        let background = FakeBackgroundScheduler()
        let model = await T54.model(transport: gate, center: center, background: background)
        await model.handleBackgroundRefresh()
        let planFetches = await gate.requests.filter { $0.url?.path() == "/plans/current" }.count
        XCTAssertEqual(planFetches, 1)
        XCTAssertEqual(background.submissions.count, 1, "the task schedules its successor")
        let pending = await center.sortedPendingIds
        let delivered = await center.delivered
        XCTAssertEqual(pending.count, 3, "the window rolls in the background too")
        XCTAssertTrue(delivered.isEmpty, "no drift source yet (T4.3): nothing posted")
    }

    func testBackgroundDriftPostsOnceAndNeverApplies() async throws {
        let center = FakeNotificationCenter()
        let alert = DriftAlert(changesetId: UUID(), projectId: T52Fixtures.portfolioId, blockStart: T53.date("2026-11-18T03:00:00Z"))
        let defaults = T54.freshDefaults()
        let model = await T54.model(transport: gate, center: center, drift: FakeDriftSource(alert: alert), defaults: defaults)
        await model.handleBackgroundRefresh()
        await model.handleBackgroundRefresh()
        let delivered = await center.delivered
        XCTAssertEqual(delivered.map(\.identifier), [DriftNotification.identifier(alert.changesetId)])
        XCTAssertEqual(T53.plain(delivered.first?.body ?? ""), "Portfolio site at 7:00 PM clashes with something new in your calendar. Tap to see a fix.")
        let confirms = await gate.requests.filter { $0.url?.path().hasSuffix("/confirm") == true }.count
        XCTAssertEqual(confirms, 0)
    }
}

// MARK: - time zone

@MainActor
final class TimezoneMonitorTests: XCTestCase {
    private var gate: GatedTransport!
    private var model: AppModel!
    private var monitor: TimezoneMonitor { model.timezone }
    private var device = T53.la

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        model = await T54.model(transport: gate, center: FakeNotificationCenter())
        monitor.deviceTimeZone = { [unowned self] in self.device }
    }

    func testNoPromptWhileTheDeviceMatchesThePlan() async {
        await model.handleForeground()
        XCTAssertNil(monitor.prompt)
    }

    func testForegroundInANewZonePrompts() async throws {
        device = TimeZone(identifier: "Asia/Tokyo")!
        await model.handleForeground()
        let prompt = try XCTUnwrap(monitor.prompt)
        XCTAssertEqual(prompt.title, "You're in a new time zone")
        XCTAssertEqual(prompt.message, "Your week was planned for Los Angeles. Want me to re-plan the rest of it for Tokyo? Times already done stay as they are.")
    }

    func testKeepItAsIsSuppressesUntilTheZoneChangesAgain() async throws {
        let plan = try T53.plan()
        device = TimeZone(identifier: "Asia/Tokyo")!
        monitor.check(plan: plan)
        monitor.keepAsIs()
        XCTAssertNil(monitor.prompt)
        monitor.check(plan: plan)
        XCTAssertNil(monitor.prompt, "same move: stays quiet")

        device = TimeZone(identifier: "Europe/London")!
        monitor.check(plan: plan)
        XCTAssertEqual(monitor.prompt?.deviceZone, "Europe/London", "a new zone asks again")

        device = T53.la
        monitor.check(plan: plan)
        XCTAssertNil(monitor.prompt)
        device = TimeZone(identifier: "Asia/Tokyo")!
        monitor.check(plan: plan)
        XCTAssertNotNil(monitor.prompt, "going home and leaving again is a new change")
    }

    func testReplanSendsPutPreferencesWithTheNewZoneAndOpensReview() async throws {
        await model.timetable.load()
        device = TimeZone(identifier: "Asia/Tokyo")!
        monitor.check(plan: model.timetable.plan)
        let staged = await monitor.replan(current: model.timetable.preferences)
        XCTAssertTrue(staged)
        let puts = await gate.requests.filter { $0.httpMethod == "PUT" && $0.url?.path() == "/preferences" }
        XCTAssertEqual(puts.count, 1)
        let sent = try JSONCoding.makeDecoder().decode(UserPreferences.self, from: XCTUnwrap(puts.first?.httpBody))
        XCTAssertEqual(sent.timezone, "Asia/Tokyo")
        XCTAssertEqual(sent.schedulableWindows, model.timetable.preferences?.schedulableWindows, "the rest of the preferences are unchanged")
        XCTAssertNotNil(puts.first?.value(forHTTPHeaderField: "X-Timezone"))
        XCTAssertNotNil(model.pending.pending, "the 202 changeset waits for review; nothing applied")
        XCTAssertNil(monitor.prompt)
        XCTAssertTrue(monitor.consumeReviewRequest())
        XCTAssertFalse(monitor.consumeReviewRequest())

        monitor.check(plan: model.timetable.plan)
        XCTAssertNil(monitor.prompt, "no second prompt while the re-plan awaits review")
    }

    func testRejectingTheReplanCountsAsKeepItAsIs() async throws {
        await model.timetable.load()
        device = TimeZone(identifier: "Asia/Tokyo")!
        monitor.check(plan: model.timetable.plan)
        await monitor.replan(current: model.timetable.preferences)
        let id = try XCTUnwrap(model.pending.pending?.id)
        await model.pending.reject(id: id)
        monitor.check(plan: model.timetable.plan)
        XCTAssertNil(monitor.prompt)
    }

    func testCityNames() {
        XCTAssertEqual(TimezoneMonitor.Prompt.city("America/Los_Angeles"), "Los Angeles")
        XCTAssertEqual(TimezoneMonitor.Prompt.city("America/Argentina/Buenos_Aires"), "Buenos Aires")
        XCTAssertEqual(TimezoneMonitor.Prompt.city("UTC"), "UTC")
    }
}

// MARK: - Google disconnected (§5)

@MainActor
final class GcalRevokedBannerTests: XCTestCase {
    func testRevokedBannerShowsOncePerRevocation() async throws {
        let gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        let defaults = T54.freshDefaults()
        let revoked = try XCTUnwrap(TestFixtures.named("get_gcal_status__revoked.json").responseBody)
        let connected = try XCTUnwrap(TestFixtures.named("get_gcal_status__connected.json").responseBody)
        let model = await T54.model(transport: gate, center: FakeNotificationCenter(), defaults: defaults)
        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("not signed in") }
        let calendar = model.calendar
        calendar.activate(userId: session.userId)

        await gate.respond("GET", "/gcal/status", body: revoked)
        await calendar.refreshStatus()
        XCTAssertEqual(calendar.status, .revoked, "the Settings row turns warning")
        XCTAssertTrue(calendar.showsRevokedBanner)
        calendar.dismissRevokedBanner()

        await gate.respond("GET", "/gcal/status", body: revoked)
        await calendar.refreshStatus()
        XCTAssertFalse(calendar.showsRevokedBanner, "same revocation: not again this session")

        let relaunched = await T54.model(transport: gate, center: FakeNotificationCenter(), defaults: defaults)
        relaunched.calendar.activate(userId: session.userId)
        await gate.respond("GET", "/gcal/status", body: revoked)
        await relaunched.calendar.refreshStatus()
        XCTAssertFalse(relaunched.calendar.showsRevokedBanner, "nor after relaunch")

        await gate.respond("GET", "/gcal/status", body: connected)
        await relaunched.calendar.refreshStatus()
        await gate.respond("GET", "/gcal/status", body: revoked)
        await relaunched.calendar.refreshStatus()
        XCTAssertTrue(relaunched.calendar.showsRevokedBanner, "a revocation after a reconnect is announced")
    }

    func testForegroundReadsGcalStatus() async throws {
        let gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        let model = await T54.model(transport: gate, center: FakeNotificationCenter())
        guard case .signedIn(let session) = model.auth.phase else { return XCTFail("not signed in") }
        model.calendar.activate(userId: session.userId)
        await gate.respond("GET", "/gcal/status", body: try XCTUnwrap(TestFixtures.named("get_gcal_status__revoked.json").responseBody))
        await model.handleForeground()
        XCTAssertTrue(model.calendar.showsRevokedBanner)
    }
}
