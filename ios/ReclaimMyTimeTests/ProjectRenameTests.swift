import Foundation
import XCTest
@testable import ReclaimMyTime

/// `PATCH /projects/{id}`: 200 + Project for a name-only edit (applied directly), 202 + Changeset
/// otherwise (contract change 2026-11-15).
@MainActor
final class ProjectRenameTests: XCTestCase {
    private var gate: GatedTransport!
    private var model: AppModel!

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        model = await T54.model(transport: gate, center: FakeNotificationCenter())
    }

    private func workoutJSON(name: String) throws -> Data {
        let listed = try T53Bodies.fixtureBody("get_projects__ok.json")["projects"] as? [[String: Any]] ?? []
        var workout = try XCTUnwrap(listed.first { ($0["id"] as? String) == T53Ids.workout.pathComponent })
        workout["name"] = name
        return try T53Bodies.data(workout)
    }

    private var patchPath: String { "/projects/\(T53Ids.workout.pathComponent)" }

    // MARK: client

    func testClientDecodes200AsAppliedProject() async throws {
        await gate.respond("PATCH", patchPath, status: 200, body: try workoutJSON(name: "Strength"))
        let result = try await model.api.updateProject(id: T53Ids.workout, patch: ProjectPatch(name: "Strength"))
        guard case .applied(let project) = result else { return XCTFail("expected applied, got \(result)") }
        XCTAssertEqual(project.name, "Strength")
    }

    func testClientDecodes202AsStagedChangeset() async throws {
        let result = try await model.api.updateProject(id: T53Ids.workout, patch: ProjectPatch(weeklyTargetHours: 3))
        guard case .staged(let changeset) = result else { return XCTFail("expected staged, got \(result)") }
        XCTAssertEqual(changeset.status, .pending)
    }

    // MARK: edit flow

    /// A pending weekly-target proposal for Workout, shown as a card under a chat entry.
    private func stagedWorkoutCard() async throws -> (Changeset, ChatModel.Entry, ProjectCard) {
        await model.timetable.load()
        let staged = try T52Fixtures.changeset("patch_projects_id__weekly_target.json")
        model.chat.debugAttach(changeset: staged, text: "Workout, 3 hours a week.")
        let entry = try XCTUnwrap(model.chat.entries.last)
        await gate.respond("GET", patchPath, body: try workoutJSON(name: "Workout"))
        await model.projects.ensureBases(for: staged)
        let card = try XCTUnwrap(model.chat.cards(for: entry).first)
        XCTAssertEqual(card.name, "Workout")
        XCTAssertEqual(model.pending.pending?.id, staged.id)
        return (staged, entry, card)
    }

    func testNameOnlyEditIsAppliedInPlaceAndLeavesThePendingChangesetAlone() async throws {
        let (staged, entry, card) = try await stagedWorkoutCard()
        let edit = ProjectEditModel(card: card)
        edit.name = "Strength"
        XCTAssertEqual(edit.makePatch(), ProjectPatch(name: "Strength"))
        await gate.respond("PATCH", patchPath, status: 200, body: try workoutJSON(name: "Strength"))

        let result = try await XCTUnwrapAsync(await edit.save(api: model.api))
        await model.chat.applyEdit(entryId: entry.id, result: result)

        XCTAssertEqual(model.pending.pending?.id, staged.id, "a direct apply doesn't touch the pending changeset")
        XCTAssertEqual(model.pending.lifecycle(of: staged.id), .pending)
        let updated = try XCTUnwrap(model.chat.entries.first { $0.id == entry.id })
        XCTAssertEqual(updated.changeset?.id, staged.id)
        XCTAssertEqual(model.chat.cards(for: updated).map(\.name), ["Strength"], "the card renames in place")
        XCTAssertEqual(model.chat.cards(for: updated).first?.weeklyTargetHours, 3, "the staged change still shows")
        XCTAssertEqual(model.timetable.projectName(T53Ids.workout), "Strength", "the project renames everywhere")
    }

    func testNonNameEditIsStagedAndReplacesThePendingChangeset() async throws {
        let (staged, entry, card) = try await stagedWorkoutCard()
        let edit = ProjectEditModel(card: card)
        edit.name = "Strength"
        edit.maxBlockMinutes = 45
        let result = try await XCTUnwrapAsync(await edit.save(api: model.api))
        guard case .staged(let replacement) = result else { return XCTFail("expected staged") }
        await model.chat.applyEdit(entryId: entry.id, result: result)
        XCTAssertEqual(model.pending.pending?.id, replacement.id, "the 202 goes through staging as before")
        XCTAssertEqual(model.chat.entries.first { $0.id == entry.id }?.changeset?.id, replacement.id)
        let patches = await gate.requests.filter { $0.httpMethod == "PATCH" && $0.url?.path() == self.patchPath }
        assertJSONEqual(try XCTUnwrap(patches.last?.httpBody), Data(#"{"name":"Strength","max_block_minutes":45}"#.utf8), "PATCH body")
        _ = staged
    }

    func testIdConflictUsesTheGenericError() async throws {
        let (staged, _, card) = try await stagedWorkoutCard()
        let edit = ProjectEditModel(card: card)
        edit.name = "Strength"
        await gate.respond("PATCH", patchPath, status: 409, body: Data(#"{"error":{"code":"project_exists","message":"x"}}"#.utf8))
        let result = await edit.save(api: model.api)
        XCTAssertNil(result)
        XCTAssertEqual(edit.errorMessage, "Couldn't save that. Nothing changed; try again.")
        XCTAssertEqual(model.pending.pending?.id, staged.id)
    }
}
