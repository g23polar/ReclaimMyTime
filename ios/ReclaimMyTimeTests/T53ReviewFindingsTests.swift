import Foundation
import Observation
import XCTest
@testable import ReclaimMyTime

/// Holds chosen requests until released and serves canned bodies; everything else goes to `inner`.
actor GatedTransport: HTTPTransport {
    let inner: any HTTPTransport
    private var holds: [String: [CheckedContinuation<Void, Never>]] = [:]
    private var armed: [String: Int] = [:]
    private var canned: [String: [(status: Int, body: Data)]] = [:]
    private(set) var requests: [URLRequest] = []

    init(inner: any HTTPTransport) {
        self.inner = inner
    }

    private static func key(_ method: String, _ path: String) -> String { "\(method) \(path)" }

    /// The next matching request waits for `release`.
    func hold(_ method: String, _ path: String) {
        armed[Self.key(method, path), default: 0] += 1
    }

    func release(_ method: String, _ path: String) {
        let key = Self.key(method, path)
        let waiting = holds[key] ?? []
        holds[key] = []
        waiting.forEach { $0.resume() }
    }

    func waitingCount(_ method: String, _ path: String) -> Int {
        holds[Self.key(method, path)]?.count ?? 0
    }

    /// The next matching request gets this reply (FIFO).
    func respond(_ method: String, _ path: String, status: Int = 200, body: Data) {
        canned[Self.key(method, path), default: []].append((status, body))
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        let key = Self.key(request.httpMethod ?? "", request.url?.path() ?? "")
        var reply: (status: Int, body: Data)?
        if var queue = canned[key], !queue.isEmpty {
            reply = queue.removeFirst()
            canned[key] = queue
        }
        if let count = armed[key], count > 0 {
            armed[key] = count - 1
            await withCheckedContinuation { continuation in
                holds[key, default: []].append(continuation)
            }
        }
        if let reply, let url = request.url,
           let response = HTTPURLResponse(url: url, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"]) {
            return (reply.body, response)
        }
        return try await inner.data(for: request)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        try await inner.stream(for: request)
    }
}

@MainActor
func waitUntil(_ condition: @MainActor () async -> Bool, timeout: Duration = .seconds(3), file: StaticString = #filePath, line: UInt = #line) async {
    let clock = ContinuousClock()
    let deadline = clock.now + timeout
    while clock.now < deadline {
        if await condition() { return }
        try? await Task.sleep(for: .milliseconds(5))
    }
    XCTFail("condition not met in time", file: file, line: line)
}

enum T53Bodies {
    static func fixtureBody(_ name: String) throws -> [String: Any] {
        let data = try XCTUnwrap(TestFixtures.named(name).responseBody)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    static func data(_ object: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: object)
    }

    /// The fixture plan with its blocks replaced (a recognisably different snapshot).
    static func plan(blocks: [[String: Any]]? = nil, transform: (([String: Any]) -> [String: Any])? = nil) throws -> Data {
        var body = try fixtureBody("get_plans_current__ok.json")
        if let blocks { body["blocks"] = blocks }
        if let transform, let current = body["blocks"] as? [[String: Any]] { body["blocks"] = current.map(transform) }
        return try data(body)
    }
}

/// T5.3 review findings (fixed in T5.4).
@MainActor
final class T53ReviewFindingsTests: XCTestCase {
    private var gate: GatedTransport!
    private var model: AppModel!
    private var timetable: TimetableModel { model.timetable }

    override func setUp() async throws {
        gate = GatedTransport(inner: MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero))
        model = makeTimetableModel(transport: gate)
    }

    private func block(lineage suffix: String) throws -> Block {
        try XCTUnwrap(timetable.plan?.blocks.first { $0.lineageId.uuidString.lowercased().hasSuffix(suffix) })
    }

    // MARK: 1. stale loads

    func testLoadStartedBeforeASkipDoesNotOverwriteTheSkippedBlock() async throws {
        await timetable.load()
        let target = try block(lineage: "03")
        await gate.hold("GET", "/plans/current")
        let refresh = Task { await timetable.load() }
        await waitUntil { await self.gate.waitingCount("GET", "/plans/current") == 1 }

        var skipped = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONCoding.makeEncoder().encode(target)) as? [String: Any])
        skipped["state"] = "skipped"
        await gate.respond("PATCH", "/blocks/\(target.id.pathComponent)", body: try T53Bodies.data(skipped))
        await timetable.skip(target)
        XCTAssertEqual(try block(lineage: "03").state, .skipped)

        await gate.release("GET", "/plans/current")
        await refresh.value
        XCTAssertEqual(try block(lineage: "03").state, .skipped, "a load issued before the skip must not publish over it")
    }

    func testOlderLoadFinishingLastIsDropped() async throws {
        await gate.respond("GET", "/plans/current", body: try T53Bodies.plan(blocks: []))
        await gate.hold("GET", "/plans/current")
        let older = Task { await timetable.load() }
        await waitUntil { await self.gate.waitingCount("GET", "/plans/current") == 1 }
        await timetable.load()
        XCTAssertEqual(timetable.plan?.blocks.count, 9)
        await gate.release("GET", "/plans/current")
        await older.value
        XCTAssertEqual(timetable.plan?.blocks.count, 9, "the older (empty) snapshot arrived last and must be dropped")
    }

    // MARK: 2. week columns follow the plan horizon

    func testWeekColumnsCoverTheWholeHorizonInAnotherTimeZone() async throws {
        await timetable.load()
        var tokyo = Calendar(identifier: .gregorian)
        tokyo.timeZone = TimeZone(identifier: "Asia/Tokyo")!
        timetable.calendar = tokyo
        let columns = timetable.columns(weekOffset: 0)
        XCTAssertEqual(columns.count, 8, "an LA Monday–Monday horizon spans 8 Tokyo dates")
        for block in timetable.plan?.blocks ?? [] {
            XCTAssertFalse(TimetableGeometry.segments(block.span, columns: columns, calendar: tokyo).isEmpty, "block \(block.lineageId) has no column")
        }
    }

    func testDSTWeekKeepsSevenColumnsInThePlanZone() throws {
        let horizon = T53.span("2026-10-26T07:00:00Z", "2026-11-02T08:00:00Z") // LA, 169 h (fall back on Sun 1 Nov)
        let columns = TimetableGeometry.columns(spanning: horizon, calendar: T53.calendar)
        XCTAssertEqual(columns.count, 7)
        XCTAssertEqual(columns.first?.start, horizon.start)
        XCTAssertEqual(columns.last?.end, horizon.end)
    }

    // MARK: 3. repeated DST hour

    func testSpanAcrossFallBackIsDrawnByElapsedTime() throws {
        let day = try XCTUnwrap(TimetableGeometry.columns(from: T53.date("2026-11-01T12:00:00Z"), calendar: T53.calendar, count: 1).first)
        // 01:30 PDT → 01:15 PST: a valid 45-minute span.
        let span = T53.span("2026-11-01T08:30:00Z", "2026-11-01T09:15:00Z")
        let segments = TimetableGeometry.segments(span, columns: [day], calendar: T53.calendar)
        XCTAssertEqual(segments.count, 1, "the block must not be dropped")
        XCTAssertEqual(segments.first.map { $0.endMinute - $0.startMinute }, 45)
        XCTAssertEqual(segments.first?.startMinute, 90)

        // 01:30 PDT → 01:30 PST: equal wall clock, 60 minutes.
        let equalClock = T53.span("2026-11-01T08:30:00Z", "2026-11-01T09:30:00Z")
        XCTAssertEqual(TimetableGeometry.segments(equalClock, columns: [day], calendar: T53.calendar).first.map { $0.endMinute - $0.startMinute }, 60)
    }

    func testFallBackDayLabelsTheRepeatedHourTwice() throws {
        let day = try XCTUnwrap(TimetableGeometry.columns(from: T53.date("2026-11-01T12:00:00Z"), calendar: T53.calendar, count: 1).first)
        let labels = (0...3).map { T53.plain(TimetableGeometry.hourLabel(hour: $0, origin: day.start, time: T53.time)) }
        XCTAssertEqual(labels, ["12 AM", "1 AM", "1 AM", "2 AM"])
        let metrics = TimetableGeometry.metrics(columns: [day], windows: nil, windowTimeZone: nil, spans: [T53.span("2026-11-02T06:00:00Z", "2026-11-02T08:00:00Z")], calendar: T53.calendar, pointsPerHour: 10)
        XCTAssertEqual(metrics.endHour, 25, "a 25-hour day keeps its last hour")
    }

    // MARK: 4. minute tick

    func testClassificationIsInvalidatedByTheClockTick() async throws {
        await timetable.load()
        let target = try block(lineage: "03")
        let changed = LockedFlag()
        withObservationTracking {
            _ = timetable.classification(for: target)
        } onChange: {
            changed.set()
        }
        timetable.advanceClock()
        XCTAssertTrue(changed.value, "views reading a classification must re-render on the minute tick")
    }

    // MARK: 5. optional requests don't hold the plan

    func testPlanPublishesWhilePinsAreStillLoading() async throws {
        await gate.hold("GET", "/pins")
        let load = Task { await timetable.load() }
        await waitUntil { self.timetable.phase == .loaded }
        XCTAssertEqual(timetable.plan?.blocks.count, 9)
        XCTAssertTrue(timetable.pins.isEmpty)
        await gate.release("GET", "/pins")
        await load.value
        XCTAssertEqual(timetable.pins.count, 1)
    }

    // MARK: 6. project cache

    func testConfirmedRenameWinsOverTheTimetableCache() async throws {
        await timetable.load()
        XCTAssertEqual(timetable.projectName(T53Ids.workout), "Workout")
        model.pending.onConfirmed?(try T53Ids.renameChangeset("Strength"))
        XCTAssertEqual(timetable.projectName(T53Ids.workout), "Strength")
    }

    func testStaleProjectsSnapshotDoesNotRevertAConfirmedRename() async throws {
        await timetable.load()
        await gate.hold("GET", "/projects")
        let refresh = Task { await timetable.load() }
        await waitUntil { await self.gate.waitingCount("GET", "/projects") == 1 }
        model.pending.onConfirmed?(try T53Ids.renameChangeset("Strength"))
        await gate.release("GET", "/projects")
        await refresh.value
        XCTAssertEqual(timetable.projectName(T53Ids.workout), "Strength")
    }

    func testProjectReadInvalidatesTheTimetableCache() async throws {
        await timetable.load()
        let listed = try T53Bodies.fixtureBody("get_projects__ok.json")["projects"] as? [[String: Any]] ?? []
        var workout = try XCTUnwrap(listed.first { ($0["id"] as? String) == T53Ids.workout.pathComponent })
        workout["name"] = "Strength"
        await gate.respond("GET", "/projects/\(T53Ids.workout.pathComponent)", body: try T53Bodies.data(workout))
        let changeset = try T52Fixtures.changeset("patch_projects_id__weekly_target.json")
        await model.projects.ensureBases(for: changeset)
        XCTAssertEqual(timetable.projectName(T53Ids.workout), "Strength")
    }
}

enum T53Ids {
    static let workout = UUID(uuidString: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b02")!

    /// `patch_projects_id__weekly_target` turned into a confirmed rename of Workout.
    static func renameChangeset(_ name: String) throws -> Changeset {
        var body = try T53Bodies.fixtureBody("patch_projects_id__weekly_target.json")
        body["status"] = "confirmed"
        body["staged"] = [[
            "kind": "project_updated", "source": "manual", "interpretation": NSNull(),
            "payload": ["project_id": workout.pathComponent, "changes": ["name": name]],
        ]]
        return try JSONCoding.makeDecoder().decode(Changeset.self, from: T53Bodies.data(body))
    }
}
