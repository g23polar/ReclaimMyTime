import XCTest
@testable import ReclaimMyTime

final class ProjectCardDerivationTests: XCTestCase {
    private let text = ProjectCardText(locale: Locale(identifier: "en_US"), calendar: Calendar(identifier: .gregorian))

    func testOnboardingFixtureYieldsOneCard() throws {
        let changeset = try T52Fixtures.changeset("post_projects__perennial_onboarding.json")
        let cards = ProjectCardDerivation.cards(from: changeset, base: { _ in nil })
        XCTAssertEqual(cards.count, 1)
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.name, "Reading")
        XCTAssertEqual(card.kind, .perennial)
        XCTAssertTrue(card.isNew)
        XCTAssertEqual(text.badge(card), "Every week")
        XCTAssertEqual(text.goalLine(card), "2 h a week")
        XCTAssertEqual(text.sessionLine(card), "Up to 1 h per sitting, at least 25 m")
    }

    func testSeveralProjectsStackInOrderAndUpdatesFold() throws {
        let changeset = T52Fixtures.staging(
            [(T52Fixtures.portfolioId, T52Fixtures.portfolioDraft), (T52Fixtures.workoutId, T52Fixtures.workoutDraft)],
            extra: [StagedMutation(source: .agent, interpretation: nil, change: .projectUpdated(projectId: T52Fixtures.portfolioId, changes: ProjectPatch(estimatedTotalHours: 25)))]
        )
        let cards = ProjectCardDerivation.cards(from: changeset, base: { _ in nil })
        XCTAssertEqual(cards.map(\.name), ["Portfolio site", "Workout"], "one card per project, first-appearance order")
        XCTAssertEqual(cards[0].estimatedTotalHours, 25, "a later update in the same changeset folds into the card")
        XCTAssertEqual(text.badge(cards[0]), "Deadline")
        let goal = try XCTUnwrap(text.goalLine(cards[0]))
        XCTAssertTrue(goal.hasPrefix("Due ") && goal.contains("Dec 12") && goal.hasSuffix(" · about 25 h in total"), goal)
        XCTAssertEqual(text.sessionLine(cards[0]), "Up to 2 h per sitting · needs laptop")
        XCTAssertEqual(text.sessionLine(cards[1]), "Up to 1 h per sitting · needs gym")
    }

    func testBareUpdateUsesKnownBaseOrShowsPartialCard() throws {
        let changeset = try T52Fixtures.changeset("patch_projects_id__weekly_target.json")
        let base = ProjectCard(id: T52Fixtures.workoutId, draft: T52Fixtures.workoutDraft, isNew: false)
        let known = ProjectCardDerivation.cards(from: changeset, base: { $0 == T52Fixtures.workoutId ? base : nil })
        XCTAssertEqual(known.first?.name, "Workout")
        XCTAssertEqual(known.first?.weeklyTargetHours, 3)
        XCTAssertEqual(known.first?.requirements, ["gym"])

        let unknown = try XCTUnwrap(ProjectCardDerivation.cards(from: changeset, base: { _ in nil }).first)
        XCTAssertEqual(text.title(unknown), "Project update")
        XCTAssertEqual(text.goalLine(unknown), "3 h a week")
    }

    func testNonProjectMutationsYieldNoCards() throws {
        let changeset = try XCTUnwrap(T52Fixtures.agentDone("post_agent_message__laptop.json").changeset)
        XCTAssertTrue(ProjectCardDerivation.cards(from: changeset, base: { _ in nil }).isEmpty)
        XCTAssertFalse(ProjectCardDerivation.hasProjectCards(changeset))
    }

    func testDurationAndAccessibilityCopy() {
        XCTAssertEqual(text.formatDuration(minutes: 45), "45 m")
        XCTAssertEqual(text.formatDuration(minutes: 90), "1 h 30 m")
        XCTAssertEqual(text.formatDuration(minutes: 120), "2 h")
        XCTAssertEqual(text.formatHours(2.5), "2.5")
        let card = ProjectCard(id: UUID(), draft: ProjectDraft(name: "Reading", goal: .perennial(weeklyTargetHours: 2), maxBlockMinutes: 90, minBlockMinutes: 25))
        XCTAssertEqual(text.accessibilityLabel(card), "Reading, every-week project, 2 hours a week, up to 1 hour 30 minutes per sitting, at least 25 minutes")
    }

    func testBannerSummary() throws {
        XCTAssertEqual(ProposalSummary.make(try T52Fixtures.changeset("put_preferences__first_week.json")).summary, "9 added")
        XCTAssertEqual(ProposalSummary.make(try T52Fixtures.changeset("post_projects__perennial_onboarding.json")).summary, "Ready to confirm your projects")
        let infeasible = ProposalSummary.make(try T52Fixtures.changeset("post_projects__deadline_infeasible.json"))
        XCTAssertTrue(infeasible.doesNotFit)
        let laptop = ProposalSummary.make(try XCTUnwrap(T52Fixtures.agentDone("post_agent_message__laptop.json").changeset))
        XCTAssertFalse(laptop.summary.contains(" 0 "), "zeros are omitted: \(laptop.summary)")
        XCTAssertTrue(laptop.summary.contains("moved"), laptop.summary)
    }
}

@MainActor
final class PendingChangesetStoreTests: XCTestCase {
    func testStageReplacesWithNewerAndIgnoresOlderOrResolved() throws {
        let h = try T52Harness()
        let older = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)], createdAt: Date(timeIntervalSince1970: 1_000))
        let newer = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)], createdAt: Date(timeIntervalSince1970: 2_000))
        XCTAssertTrue(h.pending.stage(newer))
        XCTAssertFalse(h.pending.stage(older), "an older changeset never replaces a newer one")
        XCTAssertEqual(h.pending.pending?.id, newer.id)
        XCTAssertFalse(h.pending.stage(try T52Fixtures.changeset("post_changesets_id_confirm__ok.json")), "only pending changesets are staged")

        let newest = T52Fixtures.staging([], createdAt: Date(timeIntervalSince1970: 3_000))
        XCTAssertTrue(h.pending.stage(newest))
        XCTAssertEqual(h.pending.lifecycle(of: newer.id), .superseded)
        XCTAssertEqual(h.pending.lifecycle(of: newest.id), .pending)
    }

    func testConfirmOnboardingCardsCollapsesThemAndOpensPreferences() async throws {
        let h = try T52Harness()
        let changeset = try T52Fixtures.changeset("post_projects__perennial_onboarding.json")
        h.chat.debugAttach(changeset: changeset, text: "Here's Reading.")
        let entryId = try XCTUnwrap(h.chat.entries.last?.id)
        XCTAssertEqual(h.pending.pending?.id, changeset.id)

        await h.chat.confirmCards(entryId: entryId)

        XCTAssertEqual(h.pending.lifecycle(of: changeset.id), .confirmed, "cards collapse to confirmed rows")
        XCTAssertNil(h.pending.pending, "banner clears")
        XCTAssertEqual(h.pending.liveStateRevision, 1)
        XCTAssertEqual(h.chat.entries.last?.content, .preferences, "plan-less confirm with a new project opens the preferences step")
        XCTAssertNotNil(h.chat.preferences)
        let confirms = await h.transport.requests(method: "POST", path: "/changesets/\(changeset.id.pathComponent)/confirm")
        XCTAssertEqual(confirms.count, 1)
    }

    func testConfirm409ClearsBannerTostsAndAsksForRefetch() async throws {
        let h = try T52Harness()
        await h.mock.setVariant("not_pending", for: "confirmChangeset")
        let changeset = try T52Fixtures.changeset("post_projects__perennial_onboarding.json")
        h.chat.debugAttach(changeset: changeset, text: "Here's Reading.")
        let entryId = try XCTUnwrap(h.chat.entries.last?.id)

        await h.chat.confirmCards(entryId: entryId)

        XCTAssertNil(h.pending.pending)
        XCTAssertEqual(h.pending.lifecycle(of: changeset.id), .outOfDate)
        XCTAssertEqual(h.pending.notice, .outOfDate)
        XCTAssertEqual(h.pending.notice?.message, "That proposal is out of date: your week changed since. Nothing was applied.")
        XCTAssertEqual(h.pending.liveStateRevision, 1, "readers refetch the plan")
        XCTAssertNil(h.chat.preferences, "nothing was applied, so no preferences step")
        XCTAssertNil(h.pending.actionError)
    }

    func testRejectKeepsWeekAndReleasesStagedPaletteSlot() async throws {
        let h = try T52Harness()
        let changeset = try T52Fixtures.changeset("post_projects__perennial_onboarding.json")
        h.chat.debugAttach(changeset: changeset, text: "Here's Reading.")
        let readingId = UUID(uuidString: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b03")!
        XCTAssertEqual(h.model.palette.slot(for: readingId).index, 0)

        let rejected = await h.pending.reject(id: changeset.id)
        XCTAssertTrue(rejected)
        XCTAssertEqual(h.pending.lifecycle(of: changeset.id), .rejected)
        XCTAssertNil(h.pending.pending)
        XCTAssertEqual(h.model.palette.slot(for: UUID()).index, 0, "the never-confirmed project's slot was released")
    }

    func testConfirmIgnoresNonPendingId() async throws {
        let h = try T52Harness()
        let confirmed = await h.pending.confirm(id: UUID())
        XCTAssertFalse(confirmed)
        let requests = await h.transport.requests
        XCTAssertTrue(requests.isEmpty)
    }
}

@MainActor
final class ProjectEditTests: XCTestCase {
    func testEditSendsOnlyChangedFieldsAndSupersedesInPlace() async throws {
        let h = try T52Harness()
        let original = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)])
        h.chat.debugAttach(changeset: original, text: "Workout, 4 hours a week.")
        let entry = try XCTUnwrap(h.chat.entries.last)
        let card = try XCTUnwrap(h.chat.cards(for: entry).first)

        let edit = ProjectEditModel(card: card)
        XCTAssertNil(edit.makePatch(), "no change, no PATCH")
        edit.weeklyTargetHours = 3
        XCTAssertEqual(edit.makePatch(), ProjectPatch(weeklyTargetHours: 3))

        let result = try await XCTUnwrapAsync(await edit.save(api: h.model.api))
        guard case .staged(let replacement) = result else { return XCTFail("a weekly-target edit is staged (202)") }
        await h.chat.applyEdit(entryId: entry.id, changeset: replacement)

        let patches = await h.transport.requests(method: "PATCH", path: "/projects/\(T52Fixtures.workoutId.pathComponent)")
        XCTAssertEqual(patches.count, 1)
        assertJSONEqual(try XCTUnwrap(patches.first?.httpBody), Data(#"{"weekly_target_hours":3}"#.utf8), "PATCH body")

        let updated = try XCTUnwrap(h.chat.entries.first { $0.id == entry.id })
        XCTAssertEqual(updated.changeset?.id, replacement.id, "the card group now shows the new changeset")
        XCTAssertEqual(h.pending.pending?.id, replacement.id)
        XCTAssertEqual(h.pending.lifecycle(of: original.id), .superseded)
        let cards = h.chat.cards(for: updated)
        XCTAssertEqual(cards.map(\.name), ["Workout"], "cards update in place")
        XCTAssertEqual(cards.first?.weeklyTargetHours, 3)
        XCTAssertEqual(h.chat.entries.count, 2, "no new chat entries for an edit")
    }

    func testEditValidationFailureKeepsPendingAndShowsError() async throws {
        let h = try T52Harness()
        await h.mock.setVariant("kind_mismatch", for: "updateProject")
        let original = T52Fixtures.staging([(T52Fixtures.workoutId, T52Fixtures.workoutDraft)])
        h.chat.debugAttach(changeset: original, text: "Workout.")
        let card = try XCTUnwrap(h.chat.cards(for: XCTUnwrap(h.chat.entries.last)).first)
        let edit = ProjectEditModel(card: card)
        edit.weeklyTargetHours = 5
        let result = await edit.save(api: h.model.api)
        XCTAssertNil(result)
        XCTAssertEqual(edit.errorMessage, "weekly_target_hours: only valid when kind is 'perennial'")
        XCTAssertEqual(h.pending.pending?.id, original.id)
    }

    func testDeadlineEditRoundsHoursAndMapsDate() throws {
        let card = ProjectCard(id: T52Fixtures.portfolioId, draft: T52Fixtures.portfolioDraft)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        let edit = ProjectEditModel(card: card, calendar: calendar)
        XCTAssertNil(edit.makePatch())
        edit.estimatedTotalHours = 31.04
        edit.targetDate = try XCTUnwrap(ProjectEditModel.date(from: LocalDate("2026-12-19")!, calendar: calendar))
        XCTAssertEqual(edit.makePatch(), ProjectPatch(targetDate: LocalDate("2026-12-19"), estimatedTotalHours: 31))
    }
}

func XCTUnwrapAsync<T>(_ value: T?, file: StaticString = #filePath, line: UInt = #line) async throws -> T {
    try XCTUnwrap(value, file: file, line: line)
}
