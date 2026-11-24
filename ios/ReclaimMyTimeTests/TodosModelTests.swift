import Foundation
import XCTest
@testable import ReclaimMyTime

/// Plays back scripted replies. A request is answered by the first pending script entry with the
/// same method and a path ending in the entry's path. Matching by route, not by queue position,
/// keeps the concurrent `async let` loads in `TodosModel.load()` from racing each other. A request
/// with no entry left fails with a URLError, so an unexpected call shows up as a failure.
private actor ScriptedTodosTransport: HTTPTransport {
    struct Expect: Sendable {
        var method: String
        /// The end of the request path (ids are only known once the test is running).
        var path: String
    }

    struct Reply: Sendable {
        var status: Int
        var body: Data?
    }

    private var script: [(Expect, Reply)] = []
    private(set) var requests: [URLRequest] = []

    func expect(_ method: String, _ path: String, respond status: Int = 200, body: Data? = nil) {
        script.append((Expect(method: method, path: path), Reply(status: status, body: body)))
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        let path = request.url?.path() ?? ""
        let method = request.httpMethod ?? ""
        guard let index = script.firstIndex(where: { $0.0.method == method && path.hasSuffix($0.0.path) }) else {
            throw URLError(.unsupportedURL)
        }
        let reply = script.remove(at: index).1
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        return (reply.body ?? Data(), response)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        throw URLError(.unsupportedURL)
    }

    /// The raw body of every request that carried one (Sendable; decoded on the test side).
    var sentBodies: [Data] {
        requests.compactMap(\.httpBody)
    }

    /// "METHOD /path" for every request, in order.
    var paths: [String] {
        requests.map { "\($0.httpMethod ?? "") \($0.url?.path() ?? "")" }
    }
}

/// Todo JSON for shapes the fixtures don't cover (a custom list, a created todo).
private enum TodoJSON {
    static func todo(
        id: String,
        kind: String = "one_off",
        title: String,
        position: Int = 0,
        completedAt: String? = nil,
        completedPeriods: [String] = [],
        dueDate: String? = nil,
        projectId: String? = nil,
        projectArchived: Bool = false
    ) -> [String: Any] {
        [
            "id": id, "kind": kind, "title": title, "notes": NSNull(),
            "due_date": optional(dueDate), "project_id": optional(projectId), "project_archived": projectArchived,
            "position": position, "completed_at": optional(completedAt), "completed_periods": completedPeriods,
            "created_at": "2026-11-15T03:10:00Z",
        ]
    }

    static func list(_ todos: [[String: Any]], daily: String = "2026-11-24", weekly: String = "2026-11-23") -> Data {
        data(["todos": todos, "periods": ["daily": daily, "weekly": weekly]])
    }

    static func data(_ object: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: object)
    }

    /// The fixture's `Todo` body with one field replaced.
    static func fixture(_ name: String, changing key: String, to value: Any) throws -> Data {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(TestFixtures.named(name).responseBody)) as? [String: Any])
        object[key] = value
        return data(object)
    }

    static func error(_ code: String, _ message: String) -> Data {
        data(["error": ["code": code, "message": message]])
    }

    /// Decodes a `Todo` the way the API client does.
    static func decode(_ object: [String: Any]) throws -> Todo {
        try JSONCoding.makeDecoder().decode(Todo.self, from: data(object))
    }

    private static func optional(_ value: String?) -> Any {
        value ?? NSNull()
    }
}

@MainActor
final class TodosModelTests: XCTestCase {
    private var sessionStore: SessionStore!

    /// Ids from the contract fixtures (`get_todos__ok.json`).
    private let stretch = "7e1c0a52-1d3b-4c1e-9a10-000000000001"      // daily, ticked for 2026-11-24
    private let reviewBudget = "7e1c0a52-1d3b-4c1e-9a10-000000000002"  // weekly, unticked
    private let renewPassport = "7e1c0a52-1d3b-4c1e-9a10-000000000003" // one-off, open, due 2026-12-10
    private let returnBooks = "7e1c0a52-1d3b-4c1e-9a10-000000000004"   // one-off, done 2026-11-22

    override func setUp() async throws {
        (sessionStore, _) = makeTestSessionStore()
    }

    override func tearDown() async throws {
        await sessionStore.clear()
    }

    private func makeModel(
        now: @escaping @Sendable () -> Date = { Date() },
        timeZone: @escaping @Sendable () -> TimeZone = { .current }
    ) -> (TodosModel, ScriptedTodosTransport) {
        let transport = ScriptedTodosTransport()
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: transport, sessionStore: sessionStore)
        return (TodosModel(api: api, now: now, timeZone: timeZone), transport)
    }

    /// A model loaded from the contract's list and project fixtures.
    private func loadedModel(
        now: @escaping @Sendable () -> Date = { Date() },
        timeZone: @escaping @Sendable () -> TimeZone = { .current }
    ) async throws -> (TodosModel, ScriptedTodosTransport) {
        let (model, transport) = makeModel(now: now, timeZone: timeZone)
        await transport.expect("GET", "/todos", body: try XCTUnwrap(TestFixtures.named("get_todos__ok.json").responseBody))
        await transport.expect("GET", "/projects", body: try XCTUnwrap(TestFixtures.named("get_projects__ok.json").responseBody))
        await model.load()
        return (model, transport)
    }

    private func fixtureBody(_ name: String) throws -> Data {
        try XCTUnwrap(TestFixtures.named(name).responseBody)
    }

    nonisolated static func object(_ data: Data) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    // MARK: load and sections

    func testLoadPopulatesSectionsFromFixture() async throws {
        let (model, transport) = try await loadedModel()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.today.map(\.title), ["Stretch"])
        XCTAssertEqual(model.thisWeek.map(\.title), ["Review budget"])
        XCTAssertEqual(model.openOneOffs.map(\.title), ["Renew passport"])
        XCTAssertEqual(model.doneOneOffs.map(\.title), ["Return library books"])
        XCTAssertEqual(model.periods?.daily.description, "2026-11-24")
        XCTAssertTrue(model.isTicked(model.today[0]))
        XCTAssertFalse(model.isTicked(model.thisWeek[0]))
        XCTAssertEqual(model.projectName(for: model.openOneOffs[0]), "Portfolio site")
        // The two loads run concurrently, so compare as sets.
        let paths = await transport.paths
        XCTAssertEqual(Set(paths), ["GET /todos", "GET /projects"])
        let requests = await transport.requests
        let todoRequest = try XCTUnwrap(requests.first { $0.url?.path() == "/todos" })
        XCTAssertNil(URLComponents(url: todoRequest.url!, resolvingAgainstBaseURL: false)?.queryItems,
                     "the default listing: no include_done query")
    }

    func testLoadFailureShowsFailedPhase() async throws {
        let (model, transport) = makeModel()
        await transport.expect("GET", "/todos", respond: 500, body: TodoJSON.error("internal", "boom"))
        await model.load()
        XCTAssertEqual(model.phase, .failed(.server))
        XCTAssertTrue(model.todos.isEmpty)
    }

    func testRefreshFailureKeepsLastList() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("GET", "/todos", respond: 500, body: TodoJSON.error("internal", "boom"))
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.refreshFailure, .server)
        XCTAssertEqual(model.today.count, 1)
    }

    func testOverdueOneOffIsFlaggedOnDeviceCalendar() async throws {
        let zone = try XCTUnwrap(TimeZone(identifier: "America/Los_Angeles"))
        let dec11 = try XCTUnwrap(LocalDate("2026-12-11")!.middayInstant(timeZone: zone))
        let (model, _) = try await loadedModel(now: { dec11 }, timeZone: { zone })
        XCTAssertTrue(model.isOverdue(model.openOneOffs[0]), "due 2026-12-10, today is 2026-12-11")
        XCTAssertFalse(model.isOverdue(model.doneOneOffs[0]), "a done one-off is never overdue")
    }

    func testNotOverdueOnTheDueDate() async throws {
        let zone = try XCTUnwrap(TimeZone(identifier: "America/Los_Angeles"))
        let dec10 = try XCTUnwrap(LocalDate("2026-12-10")!.middayInstant(timeZone: zone))
        let (model, _) = try await loadedModel(now: { dec10 }, timeZone: { zone })
        XCTAssertFalse(model.isOverdue(model.openOneOffs[0]))
    }

    // MARK: recurring ticks

    func testWeeklyTickSendsCurrentWeekStartAndSettles() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("PUT", "/todos/\(reviewBudget)/completions/2026-11-23", body: try fixtureBody("put_todos_id_completions_period_start__ok.json"))
        await model.toggleRecurring(model.thisWeek[0])
        XCTAssertTrue(model.isTicked(model.thisWeek[0]))
        XCTAssertNil(model.rowErrors[UUID(uuidString: reviewBudget)!])
        let paths = await transport.paths
        XCTAssertEqual(paths.last, "PUT /todos/\(reviewBudget)/completions/2026-11-23")
    }

    func testDailyUntickSendsDeleteForTheDailyPeriod() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("DELETE", "/todos/\(stretch)/completions/2026-11-24", body: try fixtureBody("delete_todos_id_completions_period_start__ok.json"))
        await model.toggleRecurring(model.today[0])
        XCTAssertFalse(model.isTicked(model.today[0]))
        let paths = await transport.paths
        XCTAssertEqual(paths.last, "DELETE /todos/\(stretch)/completions/2026-11-24")
    }

    func testTickRollsBackWithCaptionOn409() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("PUT", "/todos/\(reviewBudget)/completions/2026-11-23", respond: 409, body: TodoJSON.error("todo_not_recurring", "x"))
        await model.toggleRecurring(model.thisWeek[0])
        XCTAssertFalse(model.isTicked(model.thisWeek[0]))
        XCTAssertEqual(model.rowErrors[UUID(uuidString: reviewBudget)!], TodosModel.saveFailedCaption)
    }

    func testTickRollsBackWithCaptionOn500() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("PUT", "/todos/\(reviewBudget)/completions/2026-11-23", respond: 500, body: TodoJSON.error("internal", "x"))
        await model.toggleRecurring(model.thisWeek[0])
        XCTAssertFalse(model.isTicked(model.thisWeek[0]))
        XCTAssertEqual(model.rowErrors[UUID(uuidString: reviewBudget)!], TodosModel.saveFailedCaption)
    }

    func testCaptionClearsOnNextAttempt() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("PUT", "/todos/\(reviewBudget)/completions/2026-11-23", respond: 500, body: TodoJSON.error("internal", "x"))
        await transport.expect("PUT", "/todos/\(reviewBudget)/completions/2026-11-23", body: try fixtureBody("put_todos_id_completions_period_start__ok.json"))
        await model.toggleRecurring(model.thisWeek[0])
        XCTAssertNotNil(model.rowErrors[UUID(uuidString: reviewBudget)!])
        await model.toggleRecurring(model.thisWeek[0])
        XCTAssertNil(model.rowErrors[UUID(uuidString: reviewBudget)!])
        XCTAssertTrue(model.isTicked(model.thisWeek[0]))
    }

    func testSecondTapWhileInFlightIsIgnored() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("PUT", "/todos/\(reviewBudget)/completions/2026-11-23", body: try fixtureBody("put_todos_id_completions_period_start__ok.json"))
        let row = model.thisWeek[0]
        async let first: Void = model.toggleRecurring(row)
        async let second: Void = model.toggleRecurring(row)
        _ = await (first, second)
        let count = await transport.requests.count
        XCTAssertEqual(count, 3, "GET /todos, GET /projects, and exactly one tick")
    }

    // MARK: one-offs

    func testOneOffDoneMovesToDoneAndUndoMovesBack() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("PATCH", "/todos/\(renewPassport)", body: try fixtureBody("patch_todos_id__ok.json"))
        await model.toggleOneOff(model.openOneOffs[0])
        XCTAssertTrue(model.openOneOffs.isEmpty)
        // The server's completed_at (2026-11-24) is newer than Return library books (2026-11-22).
        XCTAssertEqual(model.doneOneOffs.map(\.title), ["Renew passport", "Return library books"])

        await transport.expect("PATCH", "/todos/\(renewPassport)", body: try TodoJSON.fixture("patch_todos_id__ok.json", changing: "completed_at", to: NSNull()))
        await model.toggleOneOff(model.doneOneOffs[0])
        XCTAssertEqual(model.openOneOffs.map(\.title), ["Renew passport"])
        let bodies = await transport.sentBodies.map(TodosModelTests.object)
        XCTAssertEqual(bodies.count, 2)
        XCTAssertEqual(bodies[0]["done"] as? Bool, true)
        XCTAssertEqual(bodies[1]["done"] as? Bool, false, "undo sends done: false")
    }

    func testOneOffRollsBackWithCaptionOn500() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("PATCH", "/todos/\(renewPassport)", respond: 500, body: TodoJSON.error("internal", "x"))
        await model.toggleOneOff(model.openOneOffs[0])
        XCTAssertEqual(model.openOneOffs.map(\.title), ["Renew passport"])
        XCTAssertNil(model.openOneOffs[0].completedAt)
        XCTAssertEqual(model.rowErrors[UUID(uuidString: renewPassport)!], TodosModel.saveFailedCaption)
    }

    // MARK: reorder

    private let a = "00000000-0000-4000-8000-000000000001"
    private let b = "00000000-0000-4000-8000-000000000002"
    private let c = "00000000-0000-4000-8000-000000000003"
    private let d = "00000000-0000-4000-8000-000000000004"

    /// One-offs a (open), d (done), b (open), c (open), positioned in that order.
    private func oneOffList(order: [(id: String, title: String, done: Bool)]) -> Data {
        TodoJSON.list(order.enumerated().map { index, item in
            TodoJSON.todo(id: item.id, title: item.title, position: index, completedAt: item.done ? "2026-11-22T10:00:00Z" : nil)
        })
    }

    private func loadOneOffs(_ model: TodosModel, _ transport: ScriptedTodosTransport, order: [(id: String, title: String, done: Bool)]) async throws {
        await transport.expect("GET", "/todos", body: oneOffList(order: order))
        await transport.expect("GET", "/projects", body: try fixtureBody("get_projects__ok.json"))
        await model.load()
    }

    func testMoveSendsOpenOneOffIdsOnly() async throws {
        let (model, transport) = makeModel()
        let initial: [(id: String, title: String, done: Bool)] = [(a, "A", false), (d, "D", true), (b, "B", false), (c, "C", false)]
        try await loadOneOffs(model, transport, order: initial)
        XCTAssertEqual(model.openOneOffs.map(\.title), ["A", "B", "C"])

        // Move C to the top of the open items. The done item is not part of the request.
        let after = TodoJSON.list([
            TodoJSON.todo(id: c, title: "C", position: 0),
            TodoJSON.todo(id: d, title: "D", position: 1, completedAt: "2026-11-22T10:00:00Z"),
            TodoJSON.todo(id: a, title: "A", position: 2),
            TodoJSON.todo(id: b, title: "B", position: 3),
        ])
        await transport.expect("PUT", "/todos/order", body: after)
        await model.move(kind: .oneOff, fromOffsets: IndexSet(integer: 2), toOffset: 0)

        XCTAssertEqual(model.openOneOffs.map(\.title), ["C", "A", "B"])
        let bodies = await transport.sentBodies.map(TodosModelTests.object)
        let sent = bodies.last
        XCTAssertEqual(sent?["kind"] as? String, "one_off")
        XCTAssertEqual(sent?["ids"] as? [String], [c, a, b], "open one-offs only, in the new order")
        XCTAssertEqual(model.doneOneOffs.map(\.title), ["D"], "the done item is untouched")
        XCTAssertNil(model.actionError)
    }

    func testMoveOfDailyTodosSendsWholeKind() async throws {
        let (model, transport) = makeModel()
        let daily1 = "00000000-0000-4000-8000-0000000000d1"
        let daily2 = "00000000-0000-4000-8000-0000000000d2"
        let daily3 = "00000000-0000-4000-8000-0000000000d3"
        await transport.expect("GET", "/todos", body: TodoJSON.list([
            TodoJSON.todo(id: daily1, kind: "daily", title: "X", position: 0),
            TodoJSON.todo(id: daily2, kind: "daily", title: "Y", position: 1),
            TodoJSON.todo(id: daily3, kind: "daily", title: "Z", position: 2),
        ]))
        await transport.expect("GET", "/projects", body: try fixtureBody("get_projects__ok.json"))
        await model.load()

        await transport.expect("PUT", "/todos/order", body: TodoJSON.list([
            TodoJSON.todo(id: daily3, kind: "daily", title: "Z", position: 0),
            TodoJSON.todo(id: daily1, kind: "daily", title: "X", position: 1),
            TodoJSON.todo(id: daily2, kind: "daily", title: "Y", position: 2),
        ]))
        await model.move(kind: .daily, fromOffsets: IndexSet(integer: 2), toOffset: 0)

        XCTAssertEqual(model.today.map(\.title), ["Z", "X", "Y"])
        let sent = await transport.sentBodies.map(TodosModelTests.object).last
        XCTAssertEqual(sent?["kind"] as? String, "daily")
        XCTAssertEqual(sent?["ids"] as? [String], [daily3, daily1, daily2], "every daily todo is open")
    }

    func testFailedReorderRestoresOrderAndShowsMessage() async throws {
        let (model, transport) = makeModel()
        try await loadOneOffs(model, transport, order: [(a, "A", false), (b, "B", false)])
        await transport.expect("PUT", "/todos/order", respond: 422, body: TodoJSON.error("invalid_order", "ids must be the whole set"))
        await model.move(kind: .oneOff, fromOffsets: IndexSet(integer: 1), toOffset: 0)
        XCTAssertEqual(model.openOneOffs.map(\.title), ["A", "B"], "rolled back")
        XCTAssertNotNil(model.actionError)
    }

    func testMoveThatChangesNothingSendsNothing() async throws {
        let (model, transport) = makeModel()
        try await loadOneOffs(model, transport, order: [(a, "A", false), (b, "B", false)])
        await model.move(kind: .oneOff, fromOffsets: IndexSet(integer: 0), toOffset: 0)
        let count = await transport.requests.count
        XCTAssertEqual(count, 2, "only the two loads")
        XCTAssertNil(model.actionError)
    }

    /// The contract's `put_todos_order__ok` request is the open one-off set: the encoded body must match it.
    func testReorderBodyMatchesContractFixture() throws {
        let fixture = try TestFixtures.named("put_todos_order__ok.json")
        let expected = try XCTUnwrap(fixture.requestBody)
        let sent = try JSONCoding.makeEncoder().encode(TodoOrder(kind: .oneOff, ids: [UUID(uuidString: renewPassport)!]))
        assertJSONEqual(sent, expected, "reorder body")
    }

    func testMovedHelperMatchesListOnMove() {
        XCTAssertEqual(TodosModel.moved(["A", "B", "C"], fromOffsets: IndexSet(integer: 0), toOffset: 3), ["B", "C", "A"])
        XCTAssertEqual(TodosModel.moved(["A", "B", "C"], fromOffsets: IndexSet(integer: 2), toOffset: 0), ["C", "A", "B"])
        XCTAssertEqual(TodosModel.moved(["A", "B", "C"], fromOffsets: IndexSet([0, 1]), toOffset: 3), ["C", "A", "B"])
    }

    // MARK: delete

    func testDeleteRemovesRow() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("DELETE", "/todos/\(reviewBudget)", respond: 204)
        await model.delete(model.thisWeek[0])
        XCTAssertTrue(model.thisWeek.isEmpty)
        XCTAssertNil(model.actionError)
    }

    func testDeleteFailureRestoresRowAndShowsMessage() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("DELETE", "/todos/\(reviewBudget)", respond: 500, body: TodoJSON.error("internal", "x"))
        await model.delete(model.thisWeek[0])
        XCTAssertEqual(model.thisWeek.map(\.title), ["Review budget"])
        XCTAssertNotNil(model.actionError)
    }

    // MARK: create

    func testCreateOneOffOmitsKindAndProjectWhenUnset() async throws {
        let (model, transport) = try await loadedModel()
        let created = TodoJSON.todo(id: "7e1c0a52-1d3b-4c1e-9a10-0000000000ff", title: "Call dentist", position: 2)
        await transport.expect("POST", "/todos", respond: 201, body: TodoJSON.data(created))
        try await model.create(TodoDraft(title: "Call dentist"))
        let body = await transport.sentBodies.map(TodosModelTests.object).last
        XCTAssertNil(body?["kind"], "one-off is the server default, so kind is omitted")
        XCTAssertNil(body?["project_id"])
        XCTAssertNil(body?["due_date"])
        XCTAssertEqual(body?["title"] as? String, "Call dentist")
        XCTAssertEqual(model.openOneOffs.last?.title, "Call dentist")
    }

    func testCreateWithProjectSendsProjectId() async throws {
        let (model, transport) = try await loadedModel()
        let project = "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b02"
        let created = TodoJSON.todo(id: "7e1c0a52-1d3b-4c1e-9a10-0000000000fe", kind: "daily", title: "Log workout", projectId: project)
        await transport.expect("POST", "/todos", respond: 201, body: TodoJSON.data(created))
        try await model.create(TodoDraft(kind: .daily, title: "Log workout", projectId: UUID(uuidString: project)))
        let body = await transport.sentBodies.map(TodosModelTests.object).last
        XCTAssertEqual(body?["kind"] as? String, "daily")
        XCTAssertEqual(body?["project_id"] as? String, project)
    }

    func testCreateValidationErrorSurfacesServerMessage() async throws {
        let (model, transport) = try await loadedModel()
        await transport.expect("POST", "/todos", respond: 422, body: TodoJSON.error("validation_failed", "due_date is for one-off todos only"))
        do {
            try await model.create(TodoDraft(kind: .daily, title: "Stretch", dueDate: LocalDate("2026-12-01")))
            XCTFail("expected the 422 to throw")
        } catch {
            XCTAssertEqual(TodosModel.editorMessage(for: error), "due_date is for one-off todos only")
        }
    }

    func testEditorMessageFallsBackWhenNoServerMessage() {
        XCTAssertEqual(TodosModel.editorMessage(for: URLError(.notConnectedToInternet)), "You're offline. Nothing was saved.")
        XCTAssertEqual(TodosModel.editorMessage(for: APIError.unexpectedStatus(502)), "Couldn't save. Try again.")
    }

    // MARK: edit payloads

    func testEditSendsOnlyChangedFields() throws {
        let original = try TodoJSON.decode(TodoJSON.todo(id: renewPassport, title: "Renew passport", dueDate: "2026-12-10",
                                                         projectId: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b01", projectArchived: true))
        // Only the title changed. The due date and the (archived) project link are unchanged, so
        // neither is re-sent: re-sending the archived link would be refused with 409 project_unavailable.
        let patch = try XCTUnwrap(TodosModel.patch(for: original, title: "Renew passport now", notes: nil,
                                                   dueDate: LocalDate("2026-12-10"), projectId: original.projectId))
        XCTAssertEqual(patch.title, "Renew passport now")
        XCTAssertNil(patch.projectId)
        XCTAssertNil(patch.dueDate)
    }

    func testEditClearingDueDateSendsNull() throws {
        let original = try TodoJSON.decode(TodoJSON.todo(id: renewPassport, title: "Renew", dueDate: "2026-12-10"))
        let patch = try XCTUnwrap(TodosModel.patch(for: original, title: "Renew", notes: nil, dueDate: nil, projectId: nil))
        XCTAssertEqual(patch.dueDate, .null)
    }

    func testRecurringEditNeverPatchesDueDate() throws {
        let original = try TodoJSON.decode(TodoJSON.todo(id: stretch, kind: "daily", title: "Stretch"))
        XCTAssertNil(TodosModel.patch(for: original, title: "Stretch", notes: nil, dueDate: LocalDate("2026-12-01"), projectId: nil))
    }

    func testUnchangedEditSendsNothing() throws {
        let original = try TodoJSON.decode(TodoJSON.todo(id: renewPassport, title: "Renew"))
        XCTAssertNil(TodosModel.patch(for: original, title: "Renew", notes: nil, dueDate: nil, projectId: nil))
    }
}
