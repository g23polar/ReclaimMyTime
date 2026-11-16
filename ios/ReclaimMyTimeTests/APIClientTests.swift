import XCTest
@testable import ReclaimMyTime

/// `URLSessionAPIClient` over a real `URLSession` with a stubbed `URLProtocol`.
final class APIClientTests: XCTestCase {
    private var sessionStore: SessionStore!
    private var keychain: KeychainStore!

    override func setUp() async throws {
        (sessionStore, keychain) = makeTestSessionStore()
        StubURLProtocol.reset()
    }

    override func tearDown() async throws {
        await sessionStore.clear()
        StubURLProtocol.reset()
    }

    private func makeClient(onUnauthenticated: (@Sendable (String?) async -> Void)? = nil) -> URLSessionAPIClient {
        URLSessionAPIClient(
            baseURL: URL(string: "https://api.example.test/v1")!,
            transport: URLSessionTransport(session: StubURLProtocol.makeSession()),
            sessionStore: sessionStore,
            timeZoneIdentifier: { "Europe/Berlin" },
            onUnauthenticated: onUnauthenticated
        )
    }

    private func signIn() async throws -> AuthSession {
        let auth = try TestFixtures.authSession()
        try await sessionStore.save(auth)
        return auth
    }

    private static func reply(_ fixture: Fixture) -> StubURLProtocol.Reply {
        StubURLProtocol.Reply(status: fixture.status, chunks: fixture.responseBody.map { [$0] } ?? [])
    }

    // MARK: headers

    func testCompleteGcalConnectPostsContractBodyWithStandardHeaders() async throws {
        let auth = try await signIn()
        let fixture = try TestFixtures.named("get_gcal_status__connected.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }

        let status = try await makeClient().completeGcalConnect(GcalCompleteRequest(authCode: "4/0Aean+x", state: "s.t="))

        XCTAssertEqual(status.connection?.status, .active)
        let (sent, body) = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(sent.url?.absoluteString, "https://api.example.test/v1/gcal/complete")
        XCTAssertEqual(sent.httpMethod, "POST")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Authorization"), "Bearer \(auth.token)")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "X-Timezone"), "Europe/Berlin")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Content-Type"), "application/json")
        assertJSONEqual(try XCTUnwrap(body), Data(#"{"auth_code":"4/0Aean+x","state":"s.t="}"#.utf8), "POST /gcal/complete body")
    }

    func testCompleteGcalConnectErrorEnvelopeSurfacesCode() async throws {
        _ = try await signIn()
        for (status, code) in [(400, "gcal_state_invalid"), (422, "gcal_scope_insufficient"), (502, "gcal_exchange_failed")] {
            let body = try JSONCoding.makeEncoder().encode(ErrorEnvelope(error: ErrorBody(code: code, message: "m")))
            StubURLProtocol.install { _, _ in StubURLProtocol.Reply(status: status, chunks: [body]) }
            do {
                _ = try await makeClient().completeGcalConnect(GcalCompleteRequest(authCode: "c", state: "s"))
                XCTFail("expected \(code)")
            } catch let error as APIError {
                XCTAssertEqual(error.serverCode, code)
            }
        }
    }

    func testAuthenticatedRequestCarriesBearerTimezoneAndAccept() async throws {
        let auth = try await signIn()
        let fixture = try TestFixtures.named("get_me__ok.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }

        let user = try await makeClient().getMe()

        XCTAssertEqual(user.id, auth.user.id)
        let request = try XCTUnwrap(StubURLProtocol.requests.first?.0)
        XCTAssertEqual(request.url?.absoluteString, "https://api.example.test/v1/me")
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(auth.token)")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Timezone"), "Europe/Berlin")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/json")
    }

    func testDefaultTimezoneIsDeviceIANAIdentifier() async throws {
        let fixture = try TestFixtures.named("get_me__ok.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }
        let client = URLSessionAPIClient(
            baseURL: URL(string: "https://api.example.test")!,
            transport: URLSessionTransport(session: StubURLProtocol.makeSession()),
            sessionStore: sessionStore
        )
        _ = try await client.getMe()
        XCTAssertEqual(StubURLProtocol.requests.first?.0.value(forHTTPHeaderField: "X-Timezone"), TimeZone.current.identifier)
    }

    func testSignInOmitsBearerAndSendsContractBody() async throws {
        _ = try await signIn() // a stale session must not leak into /auth/apple
        let fixture = try TestFixtures.named("post_auth_apple__new_user.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }

        let request = try JSONCoding.makeDecoder().decode(AppleSignInRequest.self, from: XCTUnwrap(fixture.requestBody))
        let auth = try await makeClient().signInWithApple(request)

        XCTAssertTrue(auth.isNewUser)
        let (sent, body) = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertNil(sent.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(sent.value(forHTTPHeaderField: "X-Timezone"), "Europe/Berlin")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Content-Type"), "application/json")
        assertJSONEqual(try XCTUnwrap(body), try XCTUnwrap(fixture.requestBody), "sign-in body")
    }

    func testQueryParametersAndLowercasePathIds() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("get_plans_id_diff__laptop.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }
        let planId = try XCTUnwrap(UUID(uuidString: "9C0D4E2B-6A1F-4C8E-A3B7-2F5D1E9C8A02"))
        let against = try XCTUnwrap(UUID(uuidString: "9C0D4E2B-6A1F-4C8E-A3B7-2F5D1E9C8A01"))

        _ = try await makeClient().getPlanDiff(planId: planId, against: against)

        XCTAssertEqual(
            StubURLProtocol.requests.first?.0.url?.absoluteString,
            "https://api.example.test/v1/plans/9c0d4e2b-6a1f-4c8e-a3b7-2f5d1e9c8a02/diff?against=9c0d4e2b-6a1f-4c8e-a3b7-2f5d1e9c8a01"
        )
    }

    // MARK: status handling

    func testStagedWriteDecodes202AsChangeset() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("post_projects__perennial_onboarding.json")
        XCTAssertEqual(fixture.status, 202)
        StubURLProtocol.install { _, _ in Self.reply(fixture) }
        let draft = try JSONCoding.makeDecoder().decode(ProjectDraft.self, from: XCTUnwrap(fixture.requestBody))

        let changeset = try await makeClient().createProject(draft)

        XCTAssertEqual(changeset.status, .pending)
        XCTAssertNil(changeset.plan)
        guard case .projectCreated(_, let project)? = changeset.staged.first?.change else {
            return XCTFail("expected project_created")
        }
        XCTAssertEqual(project.name, "Reading")
        XCTAssertEqual(StubURLProtocol.requests.first?.0.httpMethod, "POST")
    }

    func testPatchBlockDistinguishes200And202() async throws {
        _ = try await signIn()
        let skip = try TestFixtures.named("patch_blocks_id__skip.json")
        let delete = try TestFixtures.named("patch_blocks_id__delete_forbid.json")
        StubURLProtocol.install { _, body in
            let action = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }?["action"] as? String
            return Self.reply(action == "delete" ? delete : skip)
        }
        let client = makeClient()
        let blockId = UUID()

        guard case .applied(let block) = try await client.patchBlock(id: blockId, action: .skip) else {
            return XCTFail("skip should apply directly")
        }
        XCTAssertEqual(block.state, .skipped)

        guard case .staged(let changeset) = try await client.patchBlock(id: blockId, action: .delete) else {
            return XCTFail("delete should stage")
        }
        guard case .pinAdded(let pin)? = changeset.staged.first?.change else { return XCTFail("expected pin_added") }
        XCTAssertEqual(pin.kind, .forbid)
    }

    func testErrorEnvelopeMapsToServerError() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("post_changesets_id_confirm__not_pending.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }

        do {
            _ = try await makeClient().confirmChangeset(id: UUID())
            XCTFail("expected an error")
        } catch let APIError.server(code, message, details, status) {
            XCTAssertEqual(code, "changeset_not_pending")
            XCTAssertEqual(status, 409)
            XCTAssertFalse(message.isEmpty)
            XCTAssertNotNil(details?["status"]?.stringValue)
        }
    }

    func testValidationFailedExposesFieldErrors() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("post_projects__validation_failed.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }
        do {
            _ = try await makeClient().createProject(ProjectDraft(name: "x", goal: .perennial(weeklyTargetHours: 1), maxBlockMinutes: 30))
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertEqual(error.errorBody?.validationFields, [ValidationFieldError(path: "target_date", message: "required when kind is 'deadline'")])
        }
    }

    func testNonEnvelopeErrorIsUnexpectedStatus() async throws {
        StubURLProtocol.install { _, _ in .init(status: 502, headers: ["Content-Type": "text/html"], chunks: [Data("<html>bad gateway</html>".utf8)]) }
        do {
            _ = try await makeClient().getMe()
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertEqual(error, .unexpectedStatus(502))
        }
    }

    func testUnauthenticatedClearsSessionAndNotifies() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("get_me__unauthenticated.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }
        let notified = expectation(description: "onUnauthenticated")

        do {
            _ = try await makeClient(onUnauthenticated: { _ in notified.fulfill() }).getMe()
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertTrue(error.isUnauthenticated)
        }
        await fulfillment(of: [notified], timeout: 1)
        let remaining = try await sessionStore.current()
        XCTAssertNil(remaining)
    }

    func testAppleTokenInvalidDoesNotClearSession() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("post_auth_apple__invalid_token.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }
        do {
            _ = try await makeClient().signInWithApple(AppleSignInRequest(identityToken: "t", authorizationCode: "c", nonce: Nonce.random()))
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertEqual(error.serverCode, "apple_token_invalid")
        }
        let remaining = try await sessionStore.current()
        XCTAssertNotNil(remaining)
    }

    func testMalformedSuccessBodyIsDecodingError() async throws {
        _ = try await signIn()
        StubURLProtocol.install { _, _ in .init(status: 200, chunks: [Data(#"{"id":"nope"}"#.utf8)]) }
        do {
            _ = try await makeClient().getMe()
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .decoding = error else { return XCTFail("expected .decoding, got \(error)") }
        }
    }

    func testTransportFailureIsTransportError() async throws {
        StubURLProtocol.install { _, _ in .init(status: 0, error: URLError(.notConnectedToInternet)) }
        do {
            _ = try await makeClient().getMe()
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .transport(let urlError) = error else { return XCTFail("expected .transport, got \(error)") }
            XCTAssertEqual(urlError.code, .notConnectedToInternet)
        }
    }

    func testNoContentOperationsSucceedOn204() async throws {
        _ = try await signIn()
        StubURLProtocol.install { _, _ in .init(status: 204, headers: [:]) }
        try await makeClient().revokeCurrentSession()
        XCTAssertEqual(StubURLProtocol.requests.first?.0.url?.path(), "/v1/sessions/current")
        XCTAssertEqual(StubURLProtocol.requests.first?.0.httpMethod, "DELETE")
    }

    // MARK: SSE

    func testAgentStreamYieldsEventsAcrossChunkBoundaries() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("post_agent_message__laptop.json")
        var wire = Data()
        for event in fixture.streamEvents {
            wire.append(Data("event: \(event.event)\ndata: ".utf8))
            wire.append(event.data)
            wire.append(Data("\n\n".utf8))
        }
        let chunks = stride(from: 0, to: wire.count, by: 13).map { wire.subdata(in: $0..<min($0 + 13, wire.count)) }
        StubURLProtocol.install { _, _ in .init(status: 200, headers: ["Content-Type": "text/event-stream"], chunks: chunks) }
        let request = try JSONCoding.makeDecoder().decode(AgentMessageRequest.self, from: XCTUnwrap(fixture.requestBody))

        var events: [AgentEvent] = []
        for try await event in makeClient().postAgentMessage(request) {
            events.append(event)
        }

        let expected = try JSONCoding.makeDecoder().decode([AgentEvent].self, from: XCTUnwrap(fixture.responseBody))
        XCTAssertEqual(events, expected)
        XCTAssertEqual(StubURLProtocol.requests.first?.0.value(forHTTPHeaderField: "Accept"), "text/event-stream")
        XCTAssertNotNil(StubURLProtocol.requests.first?.0.value(forHTTPHeaderField: "Authorization"))
    }

    func testAgentPreStreamErrorIsThrown() async throws {
        _ = try await signIn()
        let fixture = try TestFixtures.named("post_agent_message__last_not_user.json")
        StubURLProtocol.install { _, _ in Self.reply(fixture) }
        do {
            for try await _ in makeClient().postAgentMessage(AgentMessageRequest(messages: [TranscriptMessage(role: .user, content: "hi")])) {}
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertEqual(error.serverCode, "validation_failed")
        }
    }

    func testAgentStreamWithoutTerminalEventThrows() async throws {
        _ = try await signIn()
        StubURLProtocol.install { _, _ in
            .init(status: 200, headers: ["Content-Type": "text/event-stream"], chunks: [Data("event: delta\ndata: {\"text\":\"Hel\"}\n\n".utf8)])
        }
        var received: [AgentEvent] = []
        do {
            for try await event in makeClient().postAgentMessage(AgentMessageRequest(messages: [TranscriptMessage(role: .user, content: "hi")])) {
                received.append(event)
            }
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertEqual(error, .streamEndedWithoutTerminalEvent)
        }
        XCTAssertEqual(received, [.delta(text: "Hel")])
    }
}
