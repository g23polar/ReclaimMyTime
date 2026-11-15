import XCTest
@testable import ReclaimMyTime

final class MockTransportTests: XCTestCase {
    private var mock: MockTransport!
    private var client: URLSessionAPIClient!
    private var sessionStore: SessionStore!

    override func setUp() async throws {
        mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: .zero)
        (sessionStore, _) = makeTestSessionStore()
        client = URLSessionAPIClient(baseURL: URL(string: AppConfig.mockBaseURL)!, transport: mock, sessionStore: sessionStore)
    }

    override func tearDown() async throws {
        await sessionStore.clear()
    }

    private func request(_ method: String, _ path: String, body: Data? = nil) -> URLRequest {
        var request = URLRequest(url: URL(string: AppConfig.mockBaseURL + path)!)
        request.httpMethod = method
        request.httpBody = body
        return request
    }

    func testEveryFixtureRouteResolvesToItsOperation() async throws {
        for fixture in try TestFixtures.all() {
            var path = fixture.path
            for (name, value) in fixture.pathParams { path = path.replacingOccurrences(of: "{\(name)}", with: value) }
            let resolved = await mock.resolve(request(fixture.method, path, body: fixture.requestBody))
            XCTAssertEqual(resolved?.operation, fixture.operation, "\(fixture.method) \(path)")
        }
    }

    func testDefaultVariantsAreDeterministicSuccessCases() async throws {
        let operations = await mock.operations
        for operation in operations where operation != "gcalCallback" {
            let fixtures = try TestFixtures.all().filter { $0.operation == operation }
            let sample = try XCTUnwrap(fixtures.first)
            var path = sample.path
            for (name, value) in sample.pathParams { path = path.replacingOccurrences(of: "{\(name)}", with: value) }
            let first = await mock.resolve(request(sample.method, path))
            let second = await mock.resolve(request(sample.method, path))
            XCTAssertEqual(first?.fileName, second?.fileName)
            XCTAssertLessThan(first?.status ?? 999, 400, "\(operation) default should be a success case")
        }
    }

    func testDefaultsServeTheStory() async throws {
        let plan = try await client.getCurrentPlan()
        XCTAssertEqual(plan.status, .live)
        XCTAssertFalse(plan.blocks.isEmpty)
        let status = try await client.getGcalStatus()
        XCTAssertEqual(status.connection?.status, .active)
        let auth = try await client.signInWithApple(AppleSignInRequest(identityToken: "x", authorizationCode: "y", nonce: Nonce.random()))
        XCTAssertTrue(auth.isNewUser)
    }

    func testOverrideHookSelectsVariant() async throws {
        await mock.setVariant("no_live_plan", for: "getCurrentPlan")
        do {
            _ = try await client.getCurrentPlan()
            XCTFail("expected no_live_plan")
        } catch let error as APIError {
            XCTAssertEqual(error.serverCode, "no_live_plan")
        }
        await mock.resetVariants()
        _ = try await client.getCurrentPlan()
    }

    func testPatchBlockRoutesByAction() async throws {
        let id = UUID()
        guard case .applied(let completed) = try await client.patchBlock(id: id, action: .complete) else { return XCTFail() }
        XCTAssertEqual(completed.state, .completed)
        guard case .staged(let forbid) = try await client.patchBlock(id: id, action: .delete) else { return XCTFail() }
        guard case .pinAdded(let payload)? = forbid.staged.first?.change else { return XCTFail() }
        XCTAssertEqual(payload.kind, .forbid)
        let span = try XCTUnwrap(Span(start: Date(timeIntervalSince1970: 1_800_000_000), end: Date(timeIntervalSince1970: 1_800_003_600)))
        guard case .staged(let pin) = try await client.patchBlock(id: id, action: .edit(span: span)) else { return XCTFail() }
        guard case .pinAdded(let pinPayload)? = pin.staged.first?.change else { return XCTFail() }
        XCTAssertEqual(pinPayload.kind, .pin)
    }

    func testUnroutedPathIs404NotFound() async throws {
        let (data, response) = try await mock.data(for: request("GET", "/nope"))
        XCTAssertEqual(response.statusCode, 404)
        XCTAssertEqual(try JSONCoding.makeDecoder().decode(ErrorEnvelope.self, from: data).error.code, "not_found")
    }

    func testSSEReplayMatchesFixture() async throws {
        let fixture = try TestFixtures.named("post_agent_message__laptop.json")
        let expected = try JSONCoding.makeDecoder().decode([AgentEvent].self, from: XCTUnwrap(fixture.responseBody))
        var events: [AgentEvent] = []
        for try await event in client.postAgentMessage(AgentMessageRequest(messages: [TranscriptMessage(role: .user, content: "no laptop")])) {
            events.append(event)
        }
        XCTAssertEqual(events, expected)
        guard case .done(let done)? = events.last else { return XCTFail("expected done") }
        XCTAssertEqual(done.outcome, .completed)
        XCTAssertNotNil(done.changeset)
    }

    func testSSEReplayOfRefusalAndUpstreamError() async throws {
        let message = AgentMessageRequest(messages: [TranscriptMessage(role: .user, content: "x")])

        await mock.setVariant("refusal", for: "postAgentMessage")
        var last: AgentEvent?
        for try await event in client.postAgentMessage(message) { last = event }
        guard case .done(let done)? = last else { return XCTFail() }
        XCTAssertEqual(done.outcome, .refused)
        XCTAssertNil(done.changeset)

        await mock.setVariant("upstream_error", for: "postAgentMessage")
        last = nil
        for try await event in client.postAgentMessage(message) { last = event }
        guard case .error(let body)? = last else { return XCTFail() }
        XCTAssertEqual(body.code, "agent_unavailable")
    }

    func testBundledMockLoadsFixtures() async throws {
        let bundled = MockTransport.bundled(frameDelay: .zero)
        let operations = await bundled.operations
        XCTAssertEqual(Set(operations), FixtureContractTests.allOperations)
    }
}
