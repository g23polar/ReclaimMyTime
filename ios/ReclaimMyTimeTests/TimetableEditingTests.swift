import Foundation
import XCTest
@testable import ReclaimMyTime

/// Throws a transport error for every request while `offline` is set.
actor SwitchableTransport: HTTPTransport {
    let inner: any HTTPTransport
    var offline = false

    init(inner: any HTTPTransport) {
        self.inner = inner
    }

    func setOffline(_ value: Bool) { offline = value }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        if offline { throw URLError(.notConnectedToInternet) }
        return try await inner.data(for: request)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        if offline { throw URLError(.notConnectedToInternet) }
        return try await inner.stream(for: request)
    }
}

@MainActor
final class TimetableEditingTests: XCTestCase {
    private var mock: MockTransport!
    private var recorder: RecordingTransport!
    private var model: AppModel!

    override func setUp() async throws {
        mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        recorder = RecordingTransport(inner: mock)
        model = makeTimetableModel(transport: recorder)
        await model.timetable.load()
    }

    private var timetable: TimetableModel { model.timetable }

    private func block(lineage suffix: String) throws -> Block {
        try XCTUnwrap(timetable.plan?.blocks.first { $0.lineageId.uuidString.lowercased().hasSuffix(suffix) })
    }

    private func patches(_ block: Block) async throws -> [BlockAction] {
        try await recorder.requests(method: "PATCH", path: "/blocks/\(block.id.pathComponent)").map {
            try JSONCoding.makeDecoder().decode(BlockAction.self, from: XCTUnwrap($0.httpBody))
        }
    }

    func testLoadJoinsPlanPinsProjectsAndPreferences() throws {
        XCTAssertEqual(timetable.phase, .loaded)
        XCTAssertEqual(timetable.plan?.blocks.count, 9)
        XCTAssertEqual(timetable.forbids.count, 1)
        XCTAssertEqual(timetable.projectName(T52Fixtures.portfolioId), "Portfolio site")
        XCTAssertEqual(timetable.preferences?.weekStart, 1)
        XCTAssertNil(timetable.busy, "G1: no busy data, so no busy layer")
        XCTAssertEqual(timetable.classification(for: try block(lineage: "01")).treatment, .completed)
        XCTAssertEqual(timetable.classification(for: try block(lineage: "03")).treatment, .planned)
    }

    func testDragEditSendsStagedPinAndOpensReview() async throws {
        let target = try block(lineage: "03")
        let span = try XCTUnwrap(Span(start: target.span.start.addingTimeInterval(3600), end: target.span.end.addingTimeInterval(3600)))
        await timetable.edit(target, to: span)

        let sent = try await patches(target)
        XCTAssertEqual(sent, [.edit(span: span)], "edit carries the dropped absolute span (D7)")
        XCTAssertEqual(model.pending.pending?.id, try T52Fixtures.changeset("patch_blocks_id__edit_pin.json").id, "the 202 lands in the pending store")
        XCTAssertTrue(model.navigation.isReviewingProposal, "review opens immediately after a drag")
        XCTAssertTrue(timetable.inFlight.isEmpty)
        XCTAssertFalse(timetable.stagedEdits.isEmpty, "the staged edit is drawn as a proposal until it resolves")
    }

    func testDeleteSendsStagedForbidAndOpensReview() async throws {
        let target = try block(lineage: "05")
        await timetable.delete(target)
        let sent = try await patches(target)
        XCTAssertEqual(sent, [.delete])
        XCTAssertEqual(model.pending.pending?.id, try T52Fixtures.changeset("patch_blocks_id__delete_forbid.json").id)
        XCTAssertTrue(model.navigation.isReviewingProposal)
    }

    func testDropIntoThePastSnapsBackWithoutARequest() async throws {
        let target = try block(lineage: "03")
        let past = try XCTUnwrap(Span(start: T53.storyNow.addingTimeInterval(-600), end: T53.storyNow.addingTimeInterval(3000)))
        await timetable.edit(target, to: past)
        let sent = try await patches(target)
        XCTAssertTrue(sent.isEmpty, "the server would 422; the client guard stops it")
        XCTAssertEqual(timetable.notice, .pastDrop)
        XCTAssertEqual(timetable.notice?.message, "You can't move things into the past.")
        XCTAssertEqual(timetable.rejectedDropCount, 1)
        XCTAssertNil(model.pending.pending)
        XCTAssertFalse(model.navigation.isReviewingProposal)
    }

    func testFrozenBlocksCannotBeEditedOrDeleted() async throws {
        let completed = try block(lineage: "01")
        let span = try XCTUnwrap(Span(start: T53.storyNow.addingTimeInterval(7200), end: T53.storyNow.addingTimeInterval(10800)))
        await timetable.edit(completed, to: span)
        await timetable.delete(completed)
        let sent = try await patches(completed)
        XCTAssertTrue(sent.isEmpty, "past/in-progress blocks are immutable (D7, block_immutable)")
        XCTAssertNil(model.pending.pending)
    }

    func testMarkDoneAndSkipOnStartedBlockApplyDirectly() async throws {
        let target = try block(lineage: "03")
        timetable.now = { target.span.start.addingTimeInterval(1800) }
        XCTAssertEqual(timetable.classification(for: target).treatment, .inProgress)
        await timetable.markDone(target)
        await timetable.skip(target)
        let sent = try await patches(target)
        XCTAssertEqual(sent, [.complete, .skip])
        XCTAssertNil(model.pending.pending, "complete/skip are not staged (D5)")
        XCTAssertFalse(model.navigation.isReviewingProposal)
    }

    func testBlockSupersededRefetchesAndShowsToast() async throws {
        let override = OverrideTransport(inner: mock)
        let model = makeTimetableModel(transport: override)
        await model.timetable.load()
        await override.override("PATCH", pathPrefix: "/blocks/", status: 409, code: KnownErrorCode.blockSuperseded)
        let target = try XCTUnwrap(model.timetable.plan?.blocks.first { model.timetable.classification(for: $0).treatment == .planned })
        let span = try XCTUnwrap(Span(start: target.span.start.addingTimeInterval(3600), end: target.span.end.addingTimeInterval(3600)))

        await model.timetable.edit(target, to: span)

        XCTAssertEqual(model.timetable.notice, .weekChanged)
        XCTAssertEqual(model.timetable.notice?.message, "Your week changed. Try that again.")
        let planFetches = await override.count(method: "GET", path: "/plans/current")
        XCTAssertEqual(planFetches, 2, "the plan is refetched after block_superseded")
        XCTAssertNil(model.pending.pending)
        XCTAssertFalse(model.navigation.isReviewingProposal)
        XCTAssertTrue(model.timetable.inFlight.isEmpty)
    }

    func testNoLivePlanAndOfflineStates() async throws {
        await mock.setVariant("no_live_plan", for: "getCurrentPlan")
        await timetable.load()
        XCTAssertEqual(timetable.phase, .noPlan)
        XCTAssertNil(timetable.plan)

        let switchable = SwitchableTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        let online = makeTimetableModel(transport: switchable)
        await online.timetable.load()
        XCTAssertEqual(online.timetable.phase, .loaded)
        await switchable.setOffline(true)
        await online.timetable.load()
        XCTAssertEqual(online.timetable.connectivity, .offline)
        XCTAssertEqual(online.timetable.phase, .loaded, "the last plan stays visible")
        XCTAssertNotNil(online.timetable.plan)

        let coldSwitch = SwitchableTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        await coldSwitch.setOffline(true)
        let cold = makeTimetableModel(transport: coldSwitch)
        await cold.timetable.load()
        XCTAssertEqual(cold.timetable.phase, .unavailable, "nothing to show yet")
    }

    func testConfirmPulsesChangesAndAsksForRefetch() async throws {
        let target = try block(lineage: "03")
        await timetable.edit(target, to: try XCTUnwrap(Span(start: target.span.start.addingTimeInterval(3600), end: target.span.end.addingTimeInterval(3600))))
        let pending = try XCTUnwrap(model.pending.pending)
        let confirmed = await model.pending.confirm(id: pending.id)
        XCTAssertTrue(confirmed)
        XCTAssertNil(model.pending.pending)
        XCTAssertEqual(model.pending.liveStateRevision, 1, "Week refetches the new live plan")
        XCTAssertEqual(timetable.pulseLineages, [UUID(uuidString: "1a2b3c4d-5e6f-4a7b-8c9d-000000000003")!], "moved blocks pulse once")
        let confirms = await recorder.requests(method: "POST", path: "/changesets/\(pending.id.pathComponent)/confirm")
        XCTAssertEqual(confirms.count, 1)
    }

    func testKeepMyWeekRejects() async throws {
        let target = try block(lineage: "05")
        await timetable.delete(target)
        let pending = try XCTUnwrap(model.pending.pending)
        let rejected = await model.pending.reject(id: pending.id)
        XCTAssertTrue(rejected)
        XCTAssertNil(model.pending.pending)
        XCTAssertTrue(timetable.pulseLineages.isEmpty)
        XCTAssertTrue(timetable.stagedEdits.isEmpty, "the proposal outline goes with the changeset")
        let rejects = await recorder.requests(method: "POST", path: "/changesets/\(pending.id.pathComponent)/reject")
        XCTAssertEqual(rejects.count, 1)
    }

    func testConfirmConflictClearsProposalAndToasts() async throws {
        await mock.setVariant("not_pending", for: "confirmChangeset")
        let target = try block(lineage: "03")
        await timetable.edit(target, to: try XCTUnwrap(Span(start: target.span.start.addingTimeInterval(3600), end: target.span.end.addingTimeInterval(3600))))
        let pending = try XCTUnwrap(model.pending.pending)
        let confirmed = await model.pending.confirm(id: pending.id)
        XCTAssertFalse(confirmed)
        XCTAssertNil(model.pending.pending, "banner clears; nothing was applied")
        XCTAssertEqual(model.pending.notice, .outOfDate)
        XCTAssertEqual(model.pending.notice?.message, "That proposal is out of date: your week changed since. Nothing was applied.")
        XCTAssertEqual(model.pending.liveStateRevision, 1, "readers refetch the plan")
        XCTAssertEqual(model.pending.lifecycle(of: pending.id), .outOfDate)
    }

    func testAgentTurnWithPlanOpensReview() async throws {
        let model = makeTimetableModel(transport: RecordingTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)))
        await model.chat.submit("No laptop today or tomorrow")
        XCTAssertNotNil(model.pending.pending?.planId)
        XCTAssertTrue(model.navigation.isReviewingProposal, "an agent changeset with a plan opens review automatically")
    }

    func testSignOutForgetsTheWeek() async throws {
        model.resetSessionState()
        XCTAssertNil(timetable.plan)
        XCTAssertEqual(timetable.phase, .idle)
        XCTAssertTrue(timetable.pins.isEmpty)
        XCTAssertFalse(model.navigation.isReviewingProposal)
    }
}

// MARK: - trade-offs

final class TradeOffCopyTests: XCTestCase {
    private let grant = UUID(uuidString: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b04")!
    private let pinId = UUID()
    private let exceptionId = UUID()
    private let relatedLineage = UUID()
    private let pinSpan = T53.span("2026-11-20T02:00:00Z", "2026-11-20T04:00:00Z") // Thu 6–8 PM

    private var copy: TradeOffCopy {
        var context = TradeOffContext()
        context.projectNames = [T52Fixtures.portfolioId: "Portfolio site", T52Fixtures.workoutId: "Workout", grant: "Grant application"]
        context.pins = [pinId: .init(projectId: T52Fixtures.portfolioId, span: pinSpan)]
        context.exceptionReasons = [exceptionId: "No laptop today or tomorrow"]
        context.lineageProjects = [relatedLineage: T52Fixtures.workoutId]
        return TradeOffCopy(time: T53.time, context: context)
    }

    private func pinViolation(_ conflict: PinConflict, kind: PinKind = .pin, related: [UUID] = []) -> Violation {
        .pinInvalidated(PinInvalidated(
            pinId: pinId, pinKind: kind, projectId: T52Fixtures.portfolioId, span: pinSpan, conflictsWith: conflict,
            exceptionId: conflict == .exception ? exceptionId : nil, relatedLineageIds: related
        ))
    }

    func testDemandShortfallCopy() {
        let text = copy.violation(.demandShortfall(DemandShortfall(projectId: grant, requiredMinutes: 1200, scheduledMinutes: 405, shortfallMinutes: 795)))
        XCTAssertEqual(text.title, "Grant application: 13 h 15 m short")
        XCTAssertEqual(text.explanation, "Needs 20 h this week; 6 h 45 m fits.")
    }

    func testEveryPinConflictHasPlainLanguageCopy() {
        let subject = "Your Thu 6:00 PM Portfolio site block"
        let keep = " Your pin is kept on record until you remove or move it."
        let expected: [PinConflict: (String, String)] = [
            .busy: ("\(subject) clashes with a meeting", "Your calendar comes first, so this block can't stay there."),
            .exception: ("\(subject) is inside time you marked unavailable", "You said you're unavailable then, which takes priority over a pinned block."),
            .window: ("\(subject) is outside your schedulable hours", "It's outside the hours you gave me."),
            .preferredWindow: ("\(subject) is outside Portfolio site's set times", "This project only runs at certain times."),
            .overlap: ("\(subject) is too close to another block", "It overlaps or leaves no breathing room before Workout."),
            .buffer: ("\(subject) is too close to another block", "It overlaps or leaves no breathing room before Workout."),
            .forbid: ("\(subject) is in time you kept free", "You removed a block from this slot earlier."),
            .horizon: ("\(subject) is outside this week", "It falls outside the week being planned."),
            .beforeNow: ("\(subject) is in the past", "That time has already gone."),
        ]
        XCTAssertEqual(Set(expected.keys), Set(PinConflict.allCases), "all 9 conflicts_with values")
        for conflict in PinConflict.allCases {
            let text = copy.violation(pinViolation(conflict, related: [relatedLineage]))
            XCTAssertEqual(T53.plain(text.title), expected[conflict]?.0, "\(conflict)")
            XCTAssertEqual(text.explanation, (expected[conflict]?.1 ?? "") + keep, "\(conflict): pins are reported, never dropped (rule 5)")
        }
        XCTAssertEqual(copy.violation(pinViolation(.overlap)).explanation, "It overlaps or leaves no breathing room before another block." + keep)
        XCTAssertEqual(T53.plain(copy.violation(pinViolation(.horizon, kind: .forbid)).title), "The Thu 6:00 PM slot you kept free is outside this week")
    }

    func testEveryRelaxationHasACardSortedByRecovery() {
        let relaxations: [Relaxation] = [
            .pauseProject(projectId: T52Fixtures.portfolioId, recoversMinutes: 120),
            .extendDeadline(projectId: grant, targetDate: LocalDate("2026-11-27")!, recoversMinutes: 795),
            .reduceEstimate(projectId: grant, estimatedTotalHours: 6.7, recoversMinutes: 795),
            .reduceWeeklyTarget(projectId: T52Fixtures.workoutId, weeklyTargetHours: 3.0, recoversMinutes: 60),
            .raiseMaxBlockMinutes(projectId: T52Fixtures.portfolioId, maxBlockMinutes: 180, recoversMinutes: 45),
            .removePin(pinId: pinId, recoversMinutes: 120),
            .removeException(exceptionId: exceptionId, recoversMinutes: 480),
        ]
        let sorted = TradeOffCopy.sorted(relaxations)
        XCTAssertEqual(sorted.map(\.recoversMinutes), [795, 795, 480, 120, 120, 60, 45])
        XCTAssertEqual(sorted.first, relaxations[1], "ties keep server order")

        let cards = Dictionary(uniqueKeysWithValues: relaxations.map { ($0, copy.relaxation($0)) })
        XCTAssertEqual(cards[relaxations[0]], RelaxationCopy(title: "Pause Portfolio site for now", detail: "Frees up 2 h"))
        XCTAssertEqual(cards[relaxations[1]], RelaxationCopy(title: "Move Grant application's deadline to Nov 27", detail: "Frees up 13 h 15 m"))
        XCTAssertEqual(cards[relaxations[2]], RelaxationCopy(title: "Plan Grant application as 6.7 h total", detail: "Frees up 13 h 15 m"))
        XCTAssertEqual(cards[relaxations[3]], RelaxationCopy(title: "Workout: 3 h a week", detail: "Frees up 1 h"))
        XCTAssertEqual(cards[relaxations[4]], RelaxationCopy(title: "Let Portfolio site run up to 3 h", detail: "Fits 45 m more"))
        XCTAssertEqual(cards[relaxations[5]].map { RelaxationCopy(title: T53.plain($0.title), detail: $0.detail) }, RelaxationCopy(title: "Unpin your Thu 6:00 PM Portfolio site block", detail: "Frees up 2 h"))
        XCTAssertEqual(cards[relaxations[6]], RelaxationCopy(title: "Drop the 'No laptop today or tomorrow' unavailability", detail: "Frees up 8 h"))
        XCTAssertEqual(TradeOffCopy.tryItConfirmation, "This will create a new proposal. Nothing changes until you confirm it.")
    }

    func testStripText() throws {
        let infeasible = try XCTUnwrap(T52Fixtures.changeset("post_projects__deadline_infeasible.json").plan?.infeasibility)
        XCTAssertEqual(copy.stripText(infeasible), "13 h 15 m doesn't fit this week")
        let pinsOnly = Infeasibility(violations: [pinViolation(.busy)], relaxations: [])
        XCTAssertEqual(copy.stripText(pinsOnly), "1 pinned block can't stay where it is")
    }

    @MainActor
    func testContextFromFixturesNamesStagedProjectsAndExceptions() async throws {
        let model = makeTimetableModel(transport: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        await model.timetable.load()
        let changeset = try T52Fixtures.changeset("post_projects__deadline_infeasible.json")
        let context = model.timetable.tradeOffContext(for: changeset)
        XCTAssertEqual(context.projectNames[grant], "Grant application", "staged projects (D23) are named from the changeset")
        XCTAssertEqual(context.projectNames[T52Fixtures.portfolioId], "Portfolio site")
        let copy = TradeOffCopy(time: T53.time, context: context)
        let titles = try XCTUnwrap(changeset.plan?.infeasibility).relaxations.map { copy.relaxation($0).title }
        XCTAssertEqual(titles, [
            "Move Grant application's deadline to Nov 27",
            "Plan Grant application as 6.7 h total",
            "Drop the 'No laptop today or tomorrow' unavailability",
            "Pause Portfolio site for now",
        ])
    }
}

@MainActor
final class TryItTests: XCTestCase {
    private let grant = UUID(uuidString: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b04")!

    private func run(_ relaxation: Relaxation) async throws -> (AppModel, RecordingTransport, Changeset) {
        let recorder = RecordingTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        let model = makeTimetableModel(transport: recorder)
        let infeasible = try T52Fixtures.changeset("post_projects__deadline_infeasible.json")
        XCTAssertTrue(model.pending.stage(infeasible))
        let outcome = await model.timetable.tryRelaxation(relaxation)
        XCTAssertEqual(outcome, .staged)
        XCTAssertNotEqual(model.pending.pending?.id, infeasible.id, "the review swaps to the new proposal")
        XCTAssertEqual(model.pending.lifecycle(of: infeasible.id), .superseded)
        let confirms = await recorder.requests.filter { $0.url?.path().contains("/changesets/") == true }
        XCTAssertTrue(confirms.isEmpty, "never applied automatically")
        return (model, recorder, infeasible)
    }

    private func body(_ request: URLRequest?) throws -> NSDictionary {
        try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request?.httpBody)) as? NSDictionary)
    }

    func testProjectRelaxationsSendMatchingPatch() async throws {
        let cases: [(Relaxation, UUID, NSDictionary)] = [
            (.extendDeadline(projectId: grant, targetDate: LocalDate("2026-11-27")!, recoversMinutes: 795), grant, ["target_date": "2026-11-27"]),
            (.reduceEstimate(projectId: grant, estimatedTotalHours: 6.7, recoversMinutes: 795), grant, ["estimated_total_hours": 6.7]),
            (.reduceWeeklyTarget(projectId: T52Fixtures.workoutId, weeklyTargetHours: 3.0, recoversMinutes: 60), T52Fixtures.workoutId, ["weekly_target_hours": 3]),
            (.raiseMaxBlockMinutes(projectId: T52Fixtures.portfolioId, maxBlockMinutes: 180, recoversMinutes: 45), T52Fixtures.portfolioId, ["max_block_minutes": 180]),
            (.pauseProject(projectId: T52Fixtures.portfolioId, recoversMinutes: 120), T52Fixtures.portfolioId, ["status": "paused"]),
        ]
        for (relaxation, projectId, expected) in cases {
            let (_, recorder, _) = try await run(relaxation)
            let patches = await recorder.requests(method: "PATCH", path: "/projects/\(projectId.pathComponent)")
            XCTAssertEqual(patches.count, 1, "\(relaxation)")
            XCTAssertEqual(try body(patches.first), expected, "\(relaxation)")
        }
    }

    func testRemovePinSendsDeletePin() async throws {
        let pinId = UUID(uuidString: "d3b8f6a1-9c2e-4a7d-b5f0-6e1c8a2d4b02")!
        let (_, recorder, _) = try await run(.removePin(pinId: pinId, recoversMinutes: 60))
        let deletes = await recorder.requests(method: "DELETE", path: "/pins/\(pinId.pathComponent)")
        XCTAssertEqual(deletes.count, 1)
    }

    func testRemoveExceptionSendsDeleteException() async throws {
        let exceptionId = UUID(uuidString: "e7c2a9d4-3f1b-4e6a-8d20-5b9c1a7e3f01")!
        let (_, recorder, _) = try await run(.removeException(exceptionId: exceptionId, recoversMinutes: 480))
        let deletes = await recorder.requests(method: "DELETE", path: "/exceptions/\(exceptionId.pathComponent)")
        XCTAssertEqual(deletes.count, 1)
    }
}
