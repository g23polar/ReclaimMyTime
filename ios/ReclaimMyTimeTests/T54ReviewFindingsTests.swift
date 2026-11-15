import Foundation
import XCTest
@testable import ReclaimMyTime

/// T5.4 review findings.
@MainActor
final class T54ReviewFindingsTests: XCTestCase {
    private var gate: GatedTransport!
    private var center: FakeNotificationCenter!
    private var model: AppModel!

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        center = FakeNotificationCenter()
        model = await T54.model(transport: gate, center: center)
    }

    // MARK: lock-screen Skip

    func testSkipRequiresDeviceAuthentication() throws {
        let start = try XCTUnwrap(NotificationCategories.all.first { $0.identifier == NotificationCategoryID.blockStart })
        let skip = try XCTUnwrap(start.actions.first { $0.identifier == NotificationActionID.skip })
        XCTAssertTrue(skip.requiresAuthentication, "Skip changes the plan: not from a locked phone")
        XCTAssertFalse(skip.opensApp)
        XCTAssertTrue(NotificationCategories.open.opensApp)
    }

    // MARK: delivered notifications on sign-out

    func testSignOutRemovesDeliveredNotificationsToo() async throws {
        await model.timetable.load()
        await model.notifications.waitForIdle()
        let start = BlockNotificationPlanner.startIdentifier(lineage: T54.lineage("03"))
        await center.deliver(start)
        try await center.add(LocalNotificationRequest(identifier: "drift.x", title: "", body: "", categoryIdentifier: "", fireDate: nil, userInfo: [:]))
        try await center.add(LocalNotificationRequest(identifier: "someone-else", title: "", body: "", categoryIdentifier: "", fireDate: nil, userInfo: [:]))

        model.resetSessionState()
        await model.notifications.waitForIdle()

        let delivered = await center.deliveredIdentifiers()
        let pending = await center.sortedPendingIds
        XCTAssertEqual(delivered, ["someone-else"], "block and drift notifications carry account data")
        XCTAssertTrue(pending.isEmpty)
    }

    // MARK: sign-out cleanup doesn't wait behind superseded rebuilds

    func testSignOutCleanupDoesNotWaitForQueuedRebuilds() async throws {
        await center.holdAdds()
        await model.timetable.load()  // queues several rebuilds; the first is stuck in `add`
        await waitUntil { await self.center.waitingAdds == 1 }

        let cleared = LockedFlag()
        let notifications = model.notifications
        model.resetSessionState()
        let cleanup = Task {
            await notifications.clear()
            cleared.set()
        }
        await waitUntil { cleared.value }
        await center.releaseAdds()
        await cleanup.value
        await notifications.waitForIdle()
        let pending = await center.sortedPendingIds
        XCTAssertTrue(pending.isEmpty, "work from the old session must not land after cleanup")
    }

    func testSupersededRebuildsAreSkipped() async throws {
        let scheduler = T54.scheduler(center: center)
        let plan = try T53.plan()
        await center.holdAdds()
        scheduler.enqueueRebuild(NotificationSnapshot(plan: plan))
        await waitUntil { await self.center.waitingAdds == 1 }
        for _ in 0..<5 { scheduler.enqueueRebuild(NotificationSnapshot(plan: plan)) }
        scheduler.enqueueRebuild(.empty)
        await center.releaseAdds()
        await scheduler.waitForIdle()
        let pending = await center.sortedPendingIds
        let adds = await center.addCount
        XCTAssertTrue(pending.isEmpty, "only the newest snapshot is applied")
        XCTAssertEqual(adds, 1, "superseded snapshots stop scheduling: only the add already in flight ran")
    }

    // MARK: timezone replan bound to its session

    func testReplanResponseAfterSignOutIsDropped() async throws {
        await model.timetable.load()
        model.timezone.deviceTimeZone = { TimeZone(identifier: "Asia/Tokyo")! }
        model.timezone.check(plan: model.timetable.plan)
        await gate.hold("PUT", "/preferences")
        let preferences = model.timetable.preferences
        let replan = Task { await self.model.timezone.replan(current: preferences) }
        await waitUntil { await self.gate.waitingCount("PUT", "/preferences") == 1 }
        model.resetSessionState()
        await gate.release("PUT", "/preferences")
        let staged = await replan.value
        XCTAssertFalse(staged)
        XCTAssertNil(model.pending.pending, "the old account's proposal must not reach the new session")
        XCTAssertFalse(model.timezone.consumeReviewRequest())
    }

    func testReplanSuspendedOnPreferencesNeverWritesAfterSignOut() async throws {
        await model.timetable.load()
        model.timezone.deviceTimeZone = { TimeZone(identifier: "Asia/Tokyo")! }
        model.timezone.check(plan: model.timetable.plan)
        await gate.hold("GET", "/preferences")
        let replan = Task { await self.model.timezone.replan(current: nil) }
        await waitUntil { await self.gate.waitingCount("GET", "/preferences") == 1 }
        model.resetSessionState()
        await gate.release("GET", "/preferences")
        _ = await replan.value
        let puts = await gate.requests.filter { $0.httpMethod == "PUT" }.count
        XCTAssertEqual(puts, 0, "no write after the session it belonged to ended")
    }

    // MARK: delayed applied rename

    func testAppliedRenameArrivingAfterSignOutIsDiscarded() async throws {
        let staged = try T52Fixtures.changeset("patch_projects_id__weekly_target.json")
        model.chat.debugAttach(changeset: staged, text: "Workout.")
        let entry = try XCTUnwrap(model.chat.entries.last)
        let listed = try T53Bodies.fixtureBody("get_projects__ok.json")["projects"] as? [[String: Any]] ?? []
        var workout = try XCTUnwrap(listed.first { ($0["id"] as? String) == T53Ids.workout.pathComponent })
        workout["name"] = "Strength"
        let renamed = try JSONCoding.makeDecoder().decode(Project.self, from: T53Bodies.data(workout))

        model.resetSessionState()
        await model.chat.applyEdit(entryId: entry.id, result: .applied(renamed))
        XCTAssertNil(model.projects.card(for: T53Ids.workout))
        XCTAssertNil(model.projects.appliedName(for: T53Ids.workout))
    }

    // MARK: project cache keeps goal data and refreshes notification copy

    private var portfolioId: UUID { T52Fixtures.portfolioId }

    private func portfolio(named name: String) throws -> Project {
        let listed = try T53Bodies.fixtureBody("get_projects__ok.json")["projects"] as? [[String: Any]] ?? []
        var body = try XCTUnwrap(listed.first { ($0["id"] as? String) == portfolioId.pathComponent })
        body["name"] = name
        return try JSONCoding.makeDecoder().decode(Project.self, from: T53Bodies.data(body))
    }

    func testAppliedRenameRefreshesNotificationCopyAndKeepsTheGoal() async throws {
        await model.timetable.load()
        await model.notifications.waitForIdle()
        model.projects.applyApplied(try portfolio(named: "Folio"))
        await model.notifications.waitForIdle()
        let body = await center.pending[BlockNotificationPlanner.startIdentifier(lineage: T54.lineage("03"))]?.body
        XCTAssertEqual(body, "Next 2 hours: Folio. That's all the time it gets today.")
        guard case .deadline? = model.notificationSnapshot().projects[portfolioId]?.goal else {
            return XCTFail("the goal (deadline copy) must survive the cache update")
        }
    }

    func testConfirmKeepsLastKnownProjectsUntilTheRefetchLands() async throws {
        await model.timetable.load()
        await gate.hold("GET", "/projects")
        model.pending.onConfirmed?(try T53Ids.renameChangeset("Strength"))
        XCTAssertEqual(model.timetable.projectName(T53Ids.workout), "Strength")
        XCTAssertNotNil(model.timetable.projects[portfolioId], "last-known goal data is kept")
        guard case .perennial? = model.timetable.projects[T53Ids.workout]?.goal else { return XCTFail("kept") }
        await model.notifications.waitForIdle()
        let workoutBlock = try XCTUnwrap(model.timetable.plan?.blocks.first { $0.lineageId == T54.lineage("04") })
        let title = await center.pending[BlockNotificationPlanner.startIdentifier(lineage: workoutBlock.lineageId)]?.title
        XCTAssertEqual(title, "Strength", "notification copy follows the confirmed name")
        await gate.release("GET", "/projects")
    }
}
