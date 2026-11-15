import XCTest
@testable import ReclaimMyTime

/// Every contracts/fixtures file decodes into the Swift type for its operation; request bodies
/// round-trip encode → decode equal (and re-encode to the same JSON); every `negative/` fixture
/// fails. The fixture list is enumerated from the bundle, so new fixtures are covered automatically.
final class FixtureContractTests: XCTestCase {
    /// Every operationId in contracts/openapi.yaml.
    static let allOperations: Set<String> = [
        "signInWithApple", "getMe", "deleteMe", "revokeAllSessions", "revokeCurrentSession",
        "listProjects", "createProject", "getProject", "updateProject", "archiveProject",
        "getPreferences", "putPreferences",
        "listExceptions", "createException", "getException", "deleteException",
        "getCurrentPlan", "getPlanDiff", "patchBlock",
        "listPins", "deletePin",
        "postAgentMessage",
        "confirmChangeset", "rejectChangeset",
        "getGcalConnectUrl", "gcalCallback", "getGcalStatus", "disconnectGcal",
    ]

    private let decoder = JSONCoding.makeDecoder()
    private let encoder = JSONCoding.makeEncoder()

    func testFixturesCoverEveryOperation() throws {
        let fixtures = try TestFixtures.all()
        XCTAssertGreaterThanOrEqual(fixtures.count, 50, "fixture bundle looks incomplete")
        XCTAssertEqual(Set(fixtures.map(\.operation)), Self.allOperations)
    }

    func testEveryFixtureResponseDecodes() throws {
        let fixtures = try TestFixtures.all()
        for fixture in fixtures {
            do {
                try decodeResponse(fixture)
            } catch {
                XCTFail("\(fixture.fileName): response failed to decode: \(error)")
            }
        }
    }

    func testEveryFixtureRequestBodyRoundTrips() throws {
        let fixtures = try TestFixtures.all()
        var roundTripped = 0
        for fixture in fixtures {
            guard let body = fixture.requestBody else { continue }
            if fixture.requestValid {
                do {
                    try roundTripRequest(fixture.operation, body: body, name: fixture.fileName)
                    roundTripped += 1
                } catch {
                    XCTFail("\(fixture.fileName): request failed to round-trip: \(error)")
                }
            } else {
                // Deliberately invalid requests must be unrepresentable client-side.
                XCTAssertThrowsError(try roundTripRequest(fixture.operation, body: body, name: fixture.fileName),
                                     "\(fixture.fileName): invalid request decoded")
            }
        }
        XCTAssertGreaterThan(roundTripped, 15)
    }

    func testEveryNegativeFixtureFails() throws {
        let negatives = try FixtureLoader.loadNegativeFixtures(from: TestFixtures.directory())
        XCTAssertFalse(negatives.isEmpty)
        for fixture in negatives {
            XCTAssertFalse(fixture.responseValid, "\(fixture.fileName) must be response_valid: false")
            XCTAssertThrowsError(try decodeResponse(fixture), "\(fixture.fileName) decoded but must fail")
        }
    }

    func testMockFixturesExcludeNegatives() throws {
        // `negative/` is a validator self-test and must never be served as mock data.
        let fixtures = try TestFixtures.all()
        XCTAssertTrue(fixtures.allSatisfy(\.responseValid))
        let negativeNames = Set(try FixtureLoader.loadNegativeFixtures(from: TestFixtures.directory()).map(\.fileName))
        XCTAssertTrue(Set(fixtures.map(\.fileName)).isDisjoint(with: negativeNames))
    }

    /// The SSE fixtures, serialized to wire format and parsed back, equal their decoded frames.
    func testEventStreamFixturesSurviveSSEWireFormat() throws {
        for fixture in try TestFixtures.all() where fixture.isEventStream {
            let expected = try decoder.decode([AgentEvent].self, from: XCTUnwrap(fixture.responseBody))
            var wire = Data(": ping\r\n\r\n".utf8)
            for event in fixture.streamEvents {
                wire.append(Data("event: \(event.event)\r\ndata: ".utf8))
                wire.append(event.data)
                wire.append(Data("\r\n\r\n".utf8))
            }
            var parser = SSEParser()
            var parsed: [AgentEvent] = []
            // Feed in 7-byte chunks so boundaries fall everywhere.
            var index = wire.startIndex
            while index < wire.endIndex {
                let end = wire.index(index, offsetBy: 7, limitedBy: wire.endIndex) ?? wire.endIndex
                for frame in parser.feed(wire[index..<end]) {
                    if let event = try AgentEvent.decode(eventName: frame.event, data: Data(frame.data.utf8), decoder: decoder) {
                        parsed.append(event)
                    }
                }
                index = end
            }
            XCTAssertEqual(parsed, expected, fixture.fileName)
        }
    }

    // MARK: dispatch

    private func decodeResponse(_ fixture: Fixture) throws {
        if fixture.status >= 400 {
            let envelope = try decoder.decode(ErrorEnvelope.self, from: XCTUnwrap(fixture.responseBody))
            XCTAssertFalse(envelope.error.code.isEmpty)
            return
        }
        if fixture.isEventStream {
            let events = try decoder.decode([AgentEvent].self, from: XCTUnwrap(fixture.responseBody))
            XCTAssertEqual(events.filter(\.isTerminal).count, 1, "\(fixture.fileName): exactly one terminal event")
            XCTAssertEqual(events.last?.isTerminal, true, "\(fixture.fileName): terminal event is last")
            return
        }
        switch fixture.status {
        case 204:
            XCTAssertNil(fixture.responseBody, fixture.fileName)
            return
        case 302:
            let location = try XCTUnwrap(fixture.responseHeaders["Location"], fixture.fileName)
            XCTAssertNotNil(GcalCallbackResult(url: try XCTUnwrap(URL(string: location))), fixture.fileName)
            return
        default:
            break
        }
        let body = try XCTUnwrap(fixture.responseBody, fixture.fileName)
        switch fixture.operation {
        case "signInWithApple": _ = try decoder.decode(AuthSession.self, from: body)
        case "getMe": _ = try decoder.decode(User.self, from: body)
        case "listProjects": _ = try decoder.decode(ProjectList.self, from: body)
        case "getProject": _ = try decoder.decode(Project.self, from: body)
        case "getPreferences": _ = try decoder.decode(UserPreferences.self, from: body)
        case "listExceptions": _ = try decoder.decode(AvailabilityExceptionList.self, from: body)
        case "getException": _ = try decoder.decode(AvailabilityException.self, from: body)
        case "getCurrentPlan": _ = try decoder.decode(WeekPlan.self, from: body)
        case "getPlanDiff": _ = try decoder.decode(PlanDiff.self, from: body)
        case "listPins": _ = try decoder.decode(PinList.self, from: body)
        case "getGcalConnectUrl": _ = try decoder.decode(GcalConnectUrl.self, from: body)
        case "getGcalStatus": _ = try decoder.decode(GcalStatus.self, from: body)
        case "patchBlock" where fixture.status == 200: _ = try decoder.decode(Block.self, from: body)
        case "patchBlock", "createProject", "updateProject", "archiveProject", "putPreferences",
             "createException", "deleteException", "deletePin", "confirmChangeset", "rejectChangeset":
            XCTAssertTrue(fixture.status == 202 || fixture.operation.hasSuffix("Changeset"), "\(fixture.fileName): staged writes are 202")
            let changeset = try decoder.decode(Changeset.self, from: body)
            // plan_id / plan / diff are all set or all null.
            XCTAssertEqual(changeset.planId == nil, changeset.plan == nil, fixture.fileName)
            XCTAssertEqual(changeset.plan == nil, changeset.diff == nil, fixture.fileName)
        default:
            XCTFail("\(fixture.fileName): no response type registered for \(fixture.operation) \(fixture.status)")
        }
    }

    private func roundTripRequest(_ operation: String, body: Data, name: String) throws {
        switch operation {
        case "signInWithApple": try roundTrip(AppleSignInRequest.self, body, name)
        case "createProject": try roundTrip(ProjectDraft.self, body, name)
        case "updateProject": try roundTrip(ProjectPatch.self, body, name)
        case "putPreferences": try roundTrip(UserPreferences.self, body, name)
        case "createException": try roundTrip(ExceptionDraft.self, body, name)
        case "patchBlock": try roundTrip(BlockAction.self, body, name)
        case "postAgentMessage": try roundTrip(AgentMessageRequest.self, body, name)
        default: XCTFail("\(name): no request type registered for \(operation)")
        }
    }

    private func roundTrip<T: Codable & Equatable>(_ type: T.Type, _ body: Data, _ name: String) throws {
        let decoded = try decoder.decode(T.self, from: body)
        let encoded = try encoder.encode(decoded)
        XCTAssertEqual(try decoder.decode(T.self, from: encoded), decoded, "\(name): encode→decode changed the value")
        assertJSONEqual(encoded, body, "\(name): re-encoded JSON differs from the fixture")
    }
}
