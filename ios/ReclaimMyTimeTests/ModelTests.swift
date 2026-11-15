import XCTest
@testable import ReclaimMyTime

/// Model behaviour not exercised by fixtures (e.g. no fixture contains `pin_invalidated`).
final class ModelTests: XCTestCase {
    private let decoder = JSONCoding.makeDecoder()
    private let encoder = JSONCoding.makeEncoder()

    func testPinInvalidatedDecodesAllNineConflictKinds() throws {
        XCTAssertEqual(PinConflict.allCases.count, 9)
        for conflict in ["busy", "exception", "window", "buffer", "overlap", "forbid", "horizon", "before_now", "preferred_window"] {
            let json = """
            {"kind":"pin_invalidated","pin_id":"d3b8f6a1-9c2e-4a7d-b5f0-6e1c8a2d4b01","pin_kind":"pin",
             "project_id":"5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b01",
             "span":{"start":"2026-11-20T02:00:00Z","end":"2026-11-20T04:00:00Z"},
             "conflicts_with":"\(conflict)","exception_id":null,
             "related_lineage_ids":["1a2b3c4d-5e6f-4a7b-8c9d-000000000002"]}
            """
            let violation = try decoder.decode(Violation.self, from: Data(json.utf8))
            guard case .pinInvalidated(let pin) = violation else { return XCTFail(conflict) }
            XCTAssertEqual(pin.conflictsWith.rawValue, conflict)
            XCTAssertEqual(pin.relatedLineageIds.count, 1)
            XCTAssertEqual(try decoder.decode(Violation.self, from: encoder.encode(violation)), violation)
        }
    }

    func testPinInvalidatedRequiresRelatedLineageIds() {
        let json = """
        {"kind":"pin_invalidated","pin_id":"d3b8f6a1-9c2e-4a7d-b5f0-6e1c8a2d4b01","pin_kind":"forbid","project_id":null,
         "span":{"start":"2026-11-20T02:00:00Z","end":"2026-11-20T04:00:00Z"},"conflicts_with":"busy","exception_id":null}
        """
        XCTAssertThrowsError(try decoder.decode(Violation.self, from: Data(json.utf8)))
    }

    func testRelaxationsRoundTrip() throws {
        let id = UUID()
        let all: [Relaxation] = [
            .extendDeadline(projectId: id, targetDate: try XCTUnwrap(LocalDate("2026-11-27")), recoversMinutes: 795),
            .reduceEstimate(projectId: id, estimatedTotalHours: 6.7, recoversMinutes: 795),
            .reduceWeeklyTarget(projectId: id, weeklyTargetHours: 3.0, recoversMinutes: 60),
            .raiseMaxBlockMinutes(projectId: id, maxBlockMinutes: 150, recoversMinutes: 30),
            .pauseProject(projectId: id, recoversMinutes: 120),
            .removePin(pinId: id, recoversMinutes: 90),
            .removeException(exceptionId: id, recoversMinutes: 480),
        ]
        for relaxation in all {
            XCTAssertEqual(try decoder.decode(Relaxation.self, from: encoder.encode(relaxation)), relaxation)
        }
        XCTAssertEqual(all.map(\.recoversMinutes), [795, 795, 60, 30, 120, 90, 480])
    }

    func testProjectPatchDistinguishesAbsentNullAndValue() throws {
        let absent = try decoder.decode(ProjectPatch.self, from: Data(#"{"name":"x"}"#.utf8))
        XCTAssertNil(absent.preferredWindows)
        let cleared = try decoder.decode(ProjectPatch.self, from: Data(#"{"preferred_windows":null}"#.utf8))
        XCTAssertEqual(cleared.preferredWindows, .null)
        assertJSONEqual(try encoder.encode(cleared), Data(#"{"preferred_windows":null}"#.utf8), "null is sent explicitly")
        assertJSONEqual(try encoder.encode(absent), Data(#"{"name":"x"}"#.utf8), "absent is omitted")
        let set = try decoder.decode(ProjectPatch.self, from: Data(#"{"preferred_windows":{"mon":[["08:00","12:00"]]}}"#.utf8))
        XCTAssertEqual(set.preferredWindows?.value?[.mon].first?.end.rawValue, "12:00")
        XCTAssertThrowsError(try decoder.decode(ProjectPatch.self, from: Data(#"{"status":"archived"}"#.utf8)))
    }

    func testProjectDiscriminatesOnKind() throws {
        let fixture = try TestFixtures.named("get_projects_id__perennial.json")
        let project = try decoder.decode(Project.self, from: XCTUnwrap(fixture.responseBody))
        guard case .perennial = project.goal else { return XCTFail("expected perennial") }
        let deadline = try decoder.decode(Project.self, from: XCTUnwrap(TestFixtures.named("get_projects_id__deadline.json").responseBody))
        guard case .deadline = deadline.goal else { return XCTFail("expected deadline") }
        // Response encoding keeps the other kind's fields as null (D9).
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(project)) as? [String: Any])
        XCTAssertTrue(json["target_date"] is NSNull)
        XCTAssertTrue(json["estimated_total_hours"] is NSNull)
    }

    func testSpanRejectsNonIncreasingRange() {
        XCTAssertThrowsError(try decoder.decode(Span.self, from: Data(#"{"start":"2026-11-20T04:00:00Z","end":"2026-11-20T04:00:00Z"}"#.utf8)))
        XCTAssertNil(Span(start: Date(timeIntervalSince1970: 10), end: Date(timeIntervalSince1970: 5)))
    }

    func testDateTimeAcceptsOffsetsAndFractionalSeconds() throws {
        let utc = try decoder.decode(Span.self, from: Data(#"{"start":"2026-11-20T02:00:00Z","end":"2026-11-20T02:00:00.250Z"}"#.utf8))
        XCTAssertEqual(utc.end.timeIntervalSince(utc.start), 0.25, accuracy: 0.0001)
        let offset = try decoder.decode(Span.self, from: Data(#"{"start":"2026-11-19T18:00:00-08:00","end":"2026-11-20T04:00:00+01:00"}"#.utf8))
        XCTAssertEqual(offset.start, utc.start)
        XCTAssertEqual(offset.end.timeIntervalSince(offset.start), 3600, accuracy: 0.0001)
        XCTAssertEqual(RFC3339.format(utc.start), "2026-11-20T02:00:00Z")
        XCTAssertEqual(RFC3339.format(utc.end), "2026-11-20T02:00:00.250Z")
    }

    func testLocalDateTimeValidation() {
        XCTAssertNotNil(LocalDate("2026-11-20"))
        XCTAssertNil(LocalDate("2026-13-01"))
        XCTAssertNil(LocalDate("20261120"))
        XCTAssertNotNil(LocalTime("24:00"))
        XCTAssertNil(LocalTime("24:01"))
        XCTAssertNil(LocalTime("7:00"))
        XCTAssertThrowsError(try decoder.decode(TimeWindow.self, from: Data(#"["22:00","08:00"]"#.utf8)))
        XCTAssertThrowsError(try decoder.decode(TimeWindow.self, from: Data(#"["08:00","09:00","10:00"]"#.utf8)))
    }

    func testWeekWindowsAbsentKeyIsEmpty() throws {
        let windows = try decoder.decode(WeekWindows.self, from: Data(#"{"mon":[["08:00","22:00"]],"tue":[]}"#.utf8))
        XCTAssertEqual(windows[.mon].count, 1)
        XCTAssertEqual(windows[.tue], [])
        XCTAssertEqual(windows[.sun], [])
        XCTAssertEqual(windows, try decoder.decode(WeekWindows.self, from: Data(#"{"mon":[["08:00","22:00"]]}"#.utf8)))
    }

    func testExceptionDraftEnforcesCheck() throws {
        let date = try XCTUnwrap(LocalDate("2026-11-20"))
        XCTAssertNil(ExceptionDraft(startDate: date, endDate: date))
        XCTAssertNil(ExceptionDraft(startDate: date, endDate: date, blocksAll: false, blocksTags: []))
        XCTAssertNotNil(ExceptionDraft(startDate: date, endDate: date, blocksTags: ["laptop"]))
        XCTAssertNotNil(ExceptionDraft(startDate: date, endDate: date, blocksAll: true))
    }

    func testDiffEntryChangeMustMatchSpans() {
        let bad = #"{"lineage_id":"1a2b3c4d-5e6f-4a7b-8c9d-000000000001","project_id":"5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b02","before":null,"after":null,"change":"moved"}"#
        XCTAssertThrowsError(try decoder.decode(DiffEntry.self, from: Data(bad.utf8)))
    }

    func testRefusalWithChangesetIsRejected() throws {
        let changeset = try XCTUnwrap(TestFixtures.named("post_changesets_id_reject__ok.json").responseBody)
        let json = #"{"outcome":"refused","reply":"no","changeset":"# + String(decoding: changeset, as: UTF8.self) + "}"
        XCTAssertThrowsError(try decoder.decode(AgentDone.self, from: Data(json.utf8)))
    }

    func testGcalCallbackURLParsing() throws {
        XCTAssertEqual(GcalCallbackResult(url: try XCTUnwrap(URL(string: "reclaimmytime://gcal/callback?result=connected"))), .connected)
        XCTAssertEqual(GcalCallbackResult(url: try XCTUnwrap(URL(string: "reclaimmytime://gcal/callback?result=error&code=gcal_access_denied"))), .error(code: "gcal_access_denied"))
        XCTAssertNil(GcalCallbackResult(url: try XCTUnwrap(URL(string: "https://example.com/gcal/callback?result=connected"))))
        XCTAssertNil(GcalCallbackResult(url: try XCTUnwrap(URL(string: "reclaimmytime://gcal/callback?result=error"))))
    }

    func testValidationFieldsTolerateMissingDetails() {
        XCTAssertEqual(ErrorBody(code: "validation_failed", message: "x").validationFields, [])
    }
}
