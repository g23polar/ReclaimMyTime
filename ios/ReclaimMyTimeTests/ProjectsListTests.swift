import Foundation
import XCTest
@testable import ReclaimMyTime

/// Serves `GET /projects` from a queue of scripted replies and records every request.
private actor ScriptedProjectsTransport: HTTPTransport {
    enum Reply: Sendable {
        case ok(Data)
        case status(Int, Data)
        case transportError(URLError)
    }

    private var replies: [Reply]
    private(set) var requests: [URLRequest] = []

    init(_ replies: [Reply]) {
        self.replies = replies
    }

    func enqueue(_ reply: Reply) {
        replies.append(reply)
    }

    /// `include_archived` of each request, in order.
    var includeArchivedValues: [String?] {
        requests.map { request in
            URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "include_archived" }?.value
        }
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        guard request.httpMethod == "GET", request.url?.path() == "/projects", !replies.isEmpty else {
            throw URLError(.unsupportedURL)
        }
        switch replies.removeFirst() {
        case .ok(let body):
            return (body, Self.response(request, 200))
        case let .status(status, body):
            return (body, Self.response(request, status))
        case .transportError(let error):
            throw error
        }
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        throw URLError(.unsupportedURL)
    }

    private static func response(_ request: URLRequest, _ status: Int) -> HTTPURLResponse {
        HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
    }
}

private enum ProjectJSON {
    static func deadline(id: UUID = UUID(), name: String = "Portfolio site", date: String = "2026-12-04", hours: Double = 18, status: String = "active") -> String {
        """
        {"id":"\(id.uuidString.lowercased())","name":"\(name)","kind":"deadline","target_date":"\(date)","estimated_total_hours":\(hours),"weekly_target_hours":null,"max_block_minutes":120,"min_block_minutes":30,"preferred_windows":null,"preferred_strict":false,"requirements":["laptop"],"status":"\(status)","created_at":"2026-11-15T02:48:12Z"}
        """
    }

    static func perennial(id: UUID = UUID(), name: String = "Reading", hours: Double = 2, status: String = "active") -> String {
        """
        {"id":"\(id.uuidString.lowercased())","name":"\(name)","kind":"perennial","target_date":null,"estimated_total_hours":null,"weekly_target_hours":\(hours),"max_block_minutes":60,"min_block_minutes":25,"preferred_windows":null,"preferred_strict":false,"requirements":[],"status":"\(status)","created_at":"2026-11-15T03:05:40Z"}
        """
    }

    static func list(_ projects: [String]) -> Data {
        Data(#"{"projects":[\#(projects.joined(separator: ","))]}"#.utf8)
    }

    static func project(_ json: String) throws -> Project {
        try JSONCoding.makeDecoder().decode(Project.self, from: Data(json.utf8))
    }

    static let serverError = Data(#"{"error":{"code":"internal","message":"test"}}"#.utf8)
}

@MainActor
final class ProjectsModelTests: XCTestCase {
    private var sessionStore: SessionStore!

    override func setUp() async throws {
        (sessionStore, _) = makeTestSessionStore()
    }

    override func tearDown() async throws {
        await sessionStore.clear()
    }

    private func makeModel(_ replies: [ScriptedProjectsTransport.Reply]) -> (ProjectsModel, ScriptedProjectsTransport) {
        let transport = ScriptedProjectsTransport(replies)
        let api = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: transport, sessionStore: sessionStore)
        return (ProjectsModel(api: api), transport)
    }

    private func fixtureList() throws -> Data {
        try XCTUnwrap(TestFixtures.named("get_projects__ok.json").responseBody)
    }

    func testStartsIdleWithArchivedOff() {
        let (model, _) = makeModel([])
        XCTAssertEqual(model.phase, .idle)
        XCTAssertFalse(model.showsArchived)
        XCTAssertTrue(model.projects.isEmpty)
    }

    func testLoadSuccessKeepsServerOrder() async throws {
        let (model, transport) = makeModel([.ok(try fixtureList())])
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.projects.map(\.name), ["Portfolio site", "Workout", "Reading"])
        XCTAssertNil(model.refreshFailure)
        let values = await transport.includeArchivedValues
        XCTAssertEqual(values, ["false"])
    }

    func testOfflineFailureThenRetry() async throws {
        let (model, transport) = makeModel([.transportError(URLError(.notConnectedToInternet)), .ok(try fixtureList())])
        await model.load()
        XCTAssertEqual(model.phase, .failed(.offline))
        XCTAssertTrue(model.projects.isEmpty)
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.projects.count, 3)
        let count = await transport.requests.count
        XCTAssertEqual(count, 2)
    }

    func testServerFailureThenRetry() async throws {
        let (model, _) = makeModel([.status(500, ProjectJSON.serverError), .ok(ProjectJSON.list([ProjectJSON.perennial()]))])
        await model.load()
        XCTAssertEqual(model.phase, .failed(.server))
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.projects.map(\.name), ["Reading"])
    }

    func testEmptyList() async {
        let (model, _) = makeModel([.ok(ProjectJSON.list([]))])
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertTrue(model.projects.isEmpty)
    }

    func testArchivedToggleChangesRequest() async throws {
        let archived = ProjectJSON.deadline(name: "Old thesis", status: "archived")
        let (model, transport) = makeModel([
            .ok(ProjectJSON.list([ProjectJSON.perennial()])),
            .ok(ProjectJSON.list([ProjectJSON.perennial(), archived])),
            .ok(ProjectJSON.list([ProjectJSON.perennial()])),
        ])
        await model.load()
        await model.setShowsArchived(true)
        XCTAssertTrue(model.showsArchived)
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.projects.map(\.status), [.active, .archived])
        await model.setShowsArchived(false)
        XCTAssertEqual(model.projects.map(\.status), [.active])
        let values = await transport.includeArchivedValues
        XCTAssertEqual(values, ["false", "true", "false"])
    }

    func testSettingSameFilterDoesNotRefetch() async throws {
        let (model, transport) = makeModel([.ok(try fixtureList())])
        await model.load()
        await model.setShowsArchived(false)
        let count = await transport.requests.count
        XCTAssertEqual(count, 1)
    }

    func testRefreshFailureKeepsLastList() async throws {
        let (model, _) = makeModel([.ok(try fixtureList()), .transportError(URLError(.timedOut))])
        await model.load()
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.projects.count, 3)
        XCTAssertEqual(model.refreshFailure, .offline)
    }

    func testToggleFailureDoesNotShowListForOtherFilter() async throws {
        let (model, transport) = makeModel([.ok(try fixtureList()), .status(500, ProjectJSON.serverError)])
        await model.load()
        await model.setShowsArchived(true)
        // The on-screen list was fetched without archived projects: show the error, not that list.
        XCTAssertEqual(model.phase, .failed(.server))
        XCTAssertNil(model.refreshFailure)
        await transport.enqueue(.ok(ProjectJSON.list([ProjectJSON.deadline(status: "archived")])))
        await model.load()
        XCTAssertEqual(model.phase, .loaded)
        XCTAssertEqual(model.projects.map(\.status), [.archived])
    }

    func testProjectMissingGoalFieldFailsTheLoad() async {
        // A deadline project without target_date violates the contract and is rejected, not shown.
        let broken = ProjectJSON.deadline().replacingOccurrences(of: #""target_date":"2026-12-04""#, with: #""target_date":null"#)
        XCTAssertTrue(broken.contains(#""target_date":null"#), "replacement must apply")
        let (model, _) = makeModel([.ok(ProjectJSON.list([broken]))])
        await model.load()
        XCTAssertEqual(model.phase, .failed(.server))
        XCTAssertTrue(model.projects.isEmpty)
    }

    func testResetClearsStateAndFilter() async throws {
        let (model, _) = makeModel([.ok(try fixtureList()), .ok(try fixtureList())])
        await model.load()
        await model.setShowsArchived(true)
        model.reset()
        XCTAssertEqual(model.phase, .idle)
        XCTAssertTrue(model.projects.isEmpty)
        XCTAssertFalse(model.showsArchived)
        XCTAssertNil(model.refreshFailure)
    }

    func testSignOutResetsProjectList() async throws {
        let harness = try T52Harness()
        await harness.model.projectList.load()
        XCTAssertEqual(harness.model.projectList.phase, .loaded)
        harness.model.resetSessionState()
        XCTAssertEqual(harness.model.projectList.phase, .idle)
        XCTAssertTrue(harness.model.projectList.projects.isEmpty)
    }
}

final class ProjectRowTextTests: XCTestCase {
    private var text: ProjectRowText {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        calendar.locale = Locale(identifier: "en_US")
        return ProjectRowText(cardText: ProjectCardText(locale: Locale(identifier: "en_US"), calendar: calendar))
    }

    func testDeadlineRow() throws {
        let project = try ProjectJSON.project(ProjectJSON.deadline(hours: 18))
        XCTAssertEqual(text.title(project), "Portfolio site")
        XCTAssertEqual(text.kindLabel(project), "Deadline")
        XCTAssertEqual(text.figure(project), "Due Fri, Dec 4 · about 18 h in total")
        XCTAssertNil(text.statusTag(project))
        XCTAssertFalse(text.isMuted(project))
        XCTAssertEqual(text.accessibilityLabel(project), "Portfolio site, deadline project, due Fri, Dec 4, about 18 hours in total")
    }

    func testDeadlineRowFractionalHours() throws {
        let project = try ProjectJSON.project(ProjectJSON.deadline(hours: 7.5))
        XCTAssertEqual(text.figure(project), "Due Fri, Dec 4 · about 7.5 h in total")
    }

    func testElapsedDueDateIsShownAsIs() throws {
        // No "Overdue" judgement: the server owns the day boundary.
        let project = try ProjectJSON.project(ProjectJSON.deadline(date: "2025-01-03"))
        XCTAssertEqual(text.figure(project), "Due Fri, Jan 3 · about 18 h in total")
        XCTAssertNil(text.statusTag(project))
    }

    func testEveryWeekRow() throws {
        let project = try ProjectJSON.project(ProjectJSON.perennial(name: "Workout", hours: 4))
        XCTAssertEqual(text.kindLabel(project), "Every week")
        XCTAssertEqual(text.figure(project), "4 h a week")
        XCTAssertEqual(text.accessibilityLabel(project), "Workout, every-week project, 4 hours a week")
    }

    func testEveryWeekRowFractionalHours() throws {
        let project = try ProjectJSON.project(ProjectJSON.perennial(hours: 1.5))
        XCTAssertEqual(text.figure(project), "1.5 h a week")
    }

    func testStatusTags() throws {
        let paused = try ProjectJSON.project(ProjectJSON.perennial(status: "paused"))
        let done = try ProjectJSON.project(ProjectJSON.deadline(status: "done"))
        let archived = try ProjectJSON.project(ProjectJSON.deadline(status: "archived"))
        XCTAssertEqual(text.statusTag(paused), "Paused")
        XCTAssertEqual(text.statusTag(done), "Done")
        XCTAssertEqual(text.statusTag(archived), "Archived")
        XCTAssertFalse(text.isMuted(paused))
        XCTAssertFalse(text.isMuted(done))
        XCTAssertTrue(text.isMuted(archived))
        XCTAssertTrue(text.accessibilityLabel(archived).hasSuffix(", archived"))
    }

    func testProjectsMissingGoalFieldsAreRejected() {
        let noDate = ProjectJSON.deadline().replacingOccurrences(of: #""target_date":"2026-12-04""#, with: #""target_date":null"#)
        let noTotal = ProjectJSON.deadline().replacingOccurrences(of: #""estimated_total_hours":18.0"#, with: #""estimated_total_hours":null"#)
        let noWeekly = ProjectJSON.perennial().replacingOccurrences(of: #""weekly_target_hours":2.0"#, with: #""weekly_target_hours":null"#)
        XCTAssertTrue(noDate.contains(#""target_date":null"#), "replacement must apply")
        XCTAssertTrue(noTotal.contains(#""estimated_total_hours":null"#), "replacement must apply")
        XCTAssertTrue(noWeekly.contains(#""weekly_target_hours":null"#), "replacement must apply")
        for json in [noDate, noTotal, noWeekly] {
            XCTAssertThrowsError(try ProjectJSON.project(json), json)
        }
    }

    /// The row reuses `ProjectCardText.goalLine`; with fields missing it shows what it has.
    func testGoalLineWithMissingOptionalFields() {
        let cardText = text.cardText
        var deadline = ProjectCard(unknownId: UUID())
        deadline.kind = .deadline
        XCTAssertNil(cardText.goalLine(deadline))
        deadline.estimatedTotalHours = 18
        XCTAssertEqual(cardText.goalLine(deadline), "about 18 h in total")
        deadline.estimatedTotalHours = nil
        deadline.targetDate = LocalDate("2026-12-04")
        XCTAssertEqual(cardText.goalLine(deadline), "Due Fri, Dec 4")

        var weekly = ProjectCard(unknownId: UUID())
        weekly.kind = .perennial
        XCTAssertNil(cardText.goalLine(weekly))
        XCTAssertNil(cardText.badge(ProjectCard(unknownId: UUID())))
    }
}
