import Foundation
import OSLog

/// The production `APIClient`. Works over any `HTTPTransport` (network or fixtures).
///
/// Every request carries `X-Timezone` (device IANA zone) and, when a session exists,
/// `Authorization: Bearer`. A 401 `unauthenticated` clears the stored session and fires
/// `onUnauthenticated` (NOTES.md: discard the token and sign in again). Tokens are never logged.
/// A 409 `gcal_reconnect_required` from a solving route (HTTP or in-stream) fires
/// `onGcalReconnectRequired` so the app can show the reconnect banner wherever the write started.
final class URLSessionAPIClient: APIClient {
    private let baseURL: URL
    private let transport: any HTTPTransport
    private let sessionStore: SessionStore
    private let timeZoneIdentifier: @Sendable () -> String
    /// Receives the rejected bearer token so auth can ignore a late notification for a session
    /// that has since been replaced.
    private let onUnauthenticated: (@Sendable (_ rejectedToken: String?) async -> Void)?
    /// Receives the bearer the refused request carried (see `onUnauthenticated`).
    private let onGcalReconnectRequired: (@Sendable (_ sentToken: String?) async -> Void)?
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "api")

    init(
        baseURL: URL,
        transport: any HTTPTransport,
        sessionStore: SessionStore,
        timeZoneIdentifier: @escaping @Sendable () -> String = { TimeZone.current.identifier },
        onUnauthenticated: (@Sendable (_ rejectedToken: String?) async -> Void)? = nil,
        onGcalReconnectRequired: (@Sendable (_ sentToken: String?) async -> Void)? = nil
    ) {
        self.baseURL = baseURL
        self.transport = transport
        self.sessionStore = sessionStore
        self.timeZoneIdentifier = timeZoneIdentifier
        self.onUnauthenticated = onUnauthenticated
        self.onGcalReconnectRequired = onGcalReconnectRequired
    }

    // MARK: auth

    func startGoogleSignIn(_ request: GoogleSignInStartRequest) async throws -> GoogleSignInStart {
        try await send(Endpoint(method: .post, path: "/auth/google/start", body: try encode(request), requiresAuth: false))
    }

    func completeGoogleSignIn(_ request: GoogleSignInCompleteRequest) async throws -> GoogleSignInResult {
        try await send(Endpoint(method: .post, path: "/auth/google/complete", body: try encode(request), requiresAuth: false))
    }

    func signInWithApple(_ request: AppleSignInRequest) async throws -> AuthSession {
        try await send(Endpoint(method: .post, path: "/auth/apple", body: try encode(request), requiresAuth: false))
    }

    func getMe() async throws -> User {
        try await send(Endpoint(method: .get, path: "/me"))
    }

    func deleteMe() async throws {
        try await sendExpectingNoContent(Endpoint(method: .delete, path: "/me"))
    }

    func revokeAllSessions() async throws {
        try await sendExpectingNoContent(Endpoint(method: .delete, path: "/sessions"))
    }

    func revokeCurrentSession() async throws {
        try await sendExpectingNoContent(Endpoint(method: .delete, path: "/sessions/current"))
    }

    func revokeSession(token: String) async throws {
        var endpoint = Endpoint(method: .delete, path: "/sessions/current")
        endpoint.bearerToken = token
        try await sendExpectingNoContent(endpoint)
    }

    // MARK: projects

    func listProjects(includeArchived: Bool) async throws -> [Project] {
        let list: ProjectList = try await send(Endpoint(
            method: .get, path: "/projects",
            query: [URLQueryItem(name: "include_archived", value: includeArchived ? "true" : "false")]
        ))
        return list.projects
    }

    func createProject(_ draft: ProjectDraft) async throws -> Changeset {
        try await send(Endpoint(method: .post, path: "/projects", body: try encode(draft)))
    }

    func getProject(id: UUID) async throws -> Project {
        try await send(Endpoint(method: .get, path: "/projects/\(id.pathComponent)"))
    }

    func updateProject(id: UUID, patch: ProjectPatch) async throws -> UpdateProjectResult {
        let (data, response) = try await perform(Endpoint(method: .patch, path: "/projects/\(id.pathComponent)", body: try encode(patch)))
        if response.statusCode == 202 {
            return .staged(try decode(Changeset.self, from: data))
        }
        return .applied(try decode(Project.self, from: data))
    }

    func archiveProject(id: UUID) async throws -> Changeset {
        try await send(Endpoint(method: .delete, path: "/projects/\(id.pathComponent)"))
    }

    // MARK: todos

    func listTodos() async throws -> TodoList {
        try await send(Endpoint(method: .get, path: "/todos"))
    }

    func createTodo(_ draft: TodoDraft) async throws -> Todo {
        try await send(Endpoint(method: .post, path: "/todos", body: try encode(draft)))
    }

    func updateTodo(id: UUID, patch: TodoPatch) async throws -> Todo {
        try await send(Endpoint(method: .patch, path: "/todos/\(id.pathComponent)", body: try encode(patch)))
    }

    func deleteTodo(id: UUID) async throws {
        try await sendExpectingNoContent(Endpoint(method: .delete, path: "/todos/\(id.pathComponent)"))
    }

    func completeTodoPeriod(id: UUID, periodStart: LocalDate) async throws -> Todo {
        try await send(Endpoint(method: .put, path: "/todos/\(id.pathComponent)/completions/\(periodStart.description)"))
    }

    func uncompleteTodoPeriod(id: UUID, periodStart: LocalDate) async throws -> Todo {
        try await send(Endpoint(method: .delete, path: "/todos/\(id.pathComponent)/completions/\(periodStart.description)"))
    }

    func reorderTodos(_ order: TodoOrder) async throws -> TodoList {
        try await send(Endpoint(method: .put, path: "/todos/order", body: try encode(order)))
    }

    // MARK: preferences

    func getPreferences() async throws -> UserPreferences {
        try await send(Endpoint(method: .get, path: "/preferences"))
    }

    func putPreferences(_ preferences: UserPreferences) async throws -> Changeset {
        try await send(Endpoint(method: .put, path: "/preferences", body: try encode(preferences)))
    }

    // MARK: exceptions

    func listExceptions(includePast: Bool) async throws -> [AvailabilityException] {
        let list: AvailabilityExceptionList = try await send(Endpoint(
            method: .get, path: "/exceptions",
            query: [URLQueryItem(name: "include_past", value: includePast ? "true" : "false")]
        ))
        return list.exceptions
    }

    func createException(_ draft: ExceptionDraft) async throws -> Changeset {
        try await send(Endpoint(method: .post, path: "/exceptions", body: try encode(draft)))
    }

    func getException(id: UUID) async throws -> AvailabilityException {
        try await send(Endpoint(method: .get, path: "/exceptions/\(id.pathComponent)"))
    }

    func deleteException(id: UUID) async throws -> Changeset {
        try await send(Endpoint(method: .delete, path: "/exceptions/\(id.pathComponent)"))
    }

    // MARK: plans & blocks

    func getCurrentPlan() async throws -> WeekPlan {
        try await send(Endpoint(method: .get, path: "/plans/current"))
    }

    func getPlanDiff(planId: UUID, against: UUID) async throws -> PlanDiff {
        try await send(Endpoint(
            method: .get, path: "/plans/\(planId.pathComponent)/diff",
            query: [URLQueryItem(name: "against", value: against.pathComponent)]
        ))
    }

    func patchBlock(id: UUID, action: BlockAction) async throws -> PatchBlockResult {
        let (data, response) = try await perform(Endpoint(method: .patch, path: "/blocks/\(id.pathComponent)", body: try encode(action)))
        if response.statusCode == 202 {
            return .staged(try decode(Changeset.self, from: data))
        }
        return .applied(try decode(Block.self, from: data))
    }

    // MARK: pins

    func listPins(includePast: Bool) async throws -> [BlockPin] {
        let list: PinList = try await send(Endpoint(
            method: .get, path: "/pins",
            query: [URLQueryItem(name: "include_past", value: includePast ? "true" : "false")]
        ))
        return list.pins
    }

    func deletePin(id: UUID) async throws -> Changeset {
        try await send(Endpoint(method: .delete, path: "/pins/\(id.pathComponent)"))
    }

    // MARK: agent (SSE)

    func postAgentMessage(_ request: AgentMessageRequest) -> AsyncThrowingStream<AgentEvent, any Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    try await self.runAgentStream(request, continuation: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func runAgentStream(
        _ request: AgentMessageRequest,
        continuation: AsyncThrowingStream<AgentEvent, any Error>.Continuation
    ) async throws {
        let endpoint = Endpoint(method: .post, path: "/agent/message", body: try encode(request), acceptsEventStream: true)
        let (urlRequest, sentToken) = try await makeAuthorizedRequest(endpoint)
        let response: HTTPURLResponse
        let chunks: AsyncThrowingStream<Data, any Error>
        do {
            (response, chunks) = try await transport.stream(for: urlRequest)
        } catch {
            throw Self.mapTransportError(error)
        }

        guard (200..<300).contains(response.statusCode) else {
            // Pre-stream failure: a plain JSON error body.
            var body = Data()
            do {
                for try await chunk in chunks { body.append(chunk) }
            } catch {
                throw Self.mapTransportError(error)
            }
            throw await handleFailure(status: response.statusCode, body: body, sentToken: sentToken, endpoint: endpoint)
        }

        let decoder = JSONCoding.makeDecoder()
        var parser = SSEParser()
        do {
            for try await chunk in chunks {
                for frame in parser.feed(chunk) {
                    let event: AgentEvent?
                    do {
                        event = try AgentEvent.decode(eventName: frame.event, data: Data(frame.data.utf8), decoder: decoder)
                    } catch {
                        throw APIError.decodingFailure(error)
                    }
                    guard let event else { continue }
                    if case .error(let body) = event, body.code == KnownErrorCode.gcalReconnectRequired {
                        await onGcalReconnectRequired?(sentToken)
                    }
                    continuation.yield(event)
                    if event.isTerminal { return } // exactly one terminal event; ignore anything after
                }
            }
        } catch let error as APIError {
            throw error
        } catch {
            throw Self.mapTransportError(error)
        }
        parser.finish()
        throw APIError.streamEndedWithoutTerminalEvent
    }

    // MARK: changesets

    func confirmChangeset(id: UUID) async throws -> Changeset {
        try await send(Endpoint(method: .post, path: "/changesets/\(id.pathComponent)/confirm"))
    }

    func rejectChangeset(id: UUID) async throws -> Changeset {
        try await send(Endpoint(method: .post, path: "/changesets/\(id.pathComponent)/reject"))
    }

    // MARK: gcal

    func getGcalConnectUrl() async throws -> GcalConnectUrl {
        try await send(Endpoint(method: .get, path: "/gcal/connect"))
    }

    func getGcalStatus() async throws -> GcalStatus {
        try await send(Endpoint(method: .get, path: "/gcal/status"))
    }

    func completeGcalConnect(_ request: GcalCompleteRequest) async throws -> GcalStatus {
        try await send(Endpoint(method: .post, path: "/gcal/complete", body: try encode(request)))
    }

    func disconnectGcal() async throws {
        try await sendExpectingNoContent(Endpoint(method: .delete, path: "/gcal"))
    }

    func syncGcal() async throws -> GcalSyncResult {
        try await send(Endpoint(method: .post, path: "/gcal/sync", reportsGcalReconnect: false))
    }

    // MARK: plumbing

    private func send<Response: Decodable>(_ endpoint: Endpoint) async throws -> Response {
        let (data, _) = try await perform(endpoint)
        return try decode(Response.self, from: data)
    }

    private func sendExpectingNoContent(_ endpoint: Endpoint) async throws {
        _ = try await perform(endpoint)
    }

    /// Sends the request; returns the body for any 2xx, throws `APIError` otherwise.
    private func perform(_ endpoint: Endpoint) async throws -> (Data, HTTPURLResponse) {
        let (request, sentToken) = try await makeAuthorizedRequest(endpoint)
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.data(for: request)
        } catch {
            throw Self.mapTransportError(error)
        }
        guard (200..<300).contains(response.statusCode) else {
            throw await handleFailure(status: response.statusCode, body: data, sentToken: sentToken, endpoint: endpoint)
        }
        return (data, response)
    }

    /// `sentToken` is the bearer the failed request carried. The local session is cleared (and auth
    /// notified) only if it is still that same session: a late 401 for a replaced session is ignored.
    private func handleFailure(status: Int, body: Data, sentToken: String?, endpoint: Endpoint) async -> APIError {
        let error = APIError.from(status: status, body: body, decoder: JSONCoding.makeDecoder())
        if endpoint.reportsGcalReconnect, error.serverCode == KnownErrorCode.gcalReconnectRequired {
            await onGcalReconnectRequired?(sentToken)
        }
        if status == 401, error.isUnauthenticated {
            if await sessionStore.clear(ifToken: sentToken) {
                logger.notice("Session rejected by server (401 unauthenticated); cleared local session")
                await onUnauthenticated?(sentToken)
            } else {
                logger.notice("Ignoring 401 for a session that is no longer current")
            }
        }
        return error
    }

    func makeURLRequest(_ endpoint: Endpoint) async throws -> URLRequest {
        try await makeAuthorizedRequest(endpoint).request
    }

    /// Builds the request and reports which bearer token (if any) it carries.
    private func makeAuthorizedRequest(_ endpoint: Endpoint) async throws -> (request: URLRequest, token: String?) {
        var token: String?
        if endpoint.requiresAuth {
            if let explicit = endpoint.bearerToken {
                token = explicit
            } else {
                token = await sessionStore.token()
            }
        }
        return (try buildURLRequest(endpoint, token: token), token)
    }

    private func buildURLRequest(_ endpoint: Endpoint, token: String?) throws -> URLRequest {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw APIError.encoding("Invalid base URL")
        }
        let basePath = components.path.hasSuffix("/") ? String(components.path.dropLast()) : components.path
        components.path = basePath + endpoint.path
        if !endpoint.query.isEmpty { components.queryItems = endpoint.query }
        guard let url = components.url else { throw APIError.encoding("Invalid path \(endpoint.path)") }

        var request = URLRequest(url: url)
        request.httpMethod = endpoint.method.rawValue
        request.setValue(endpoint.acceptsEventStream ? "text/event-stream" : "application/json", forHTTPHeaderField: "Accept")
        request.setValue(timeZoneIdentifier(), forHTTPHeaderField: "X-Timezone")
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body = endpoint.body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func encode<Body: Encodable>(_ body: Body) throws -> Data {
        do {
            return try JSONCoding.makeEncoder().encode(body)
        } catch {
            throw APIError.encoding(String(describing: error))
        }
    }

    private func decode<Response: Decodable>(_ type: Response.Type, from data: Data) throws -> Response {
        do {
            return try JSONCoding.makeDecoder().decode(type, from: data)
        } catch {
            throw APIError.decodingFailure(error)
        }
    }

    private static func mapTransportError(_ error: any Error) -> any Error {
        if error is CancellationError { return error }
        if let apiError = error as? APIError { return apiError }
        if let urlError = error as? URLError {
            return urlError.code == .cancelled ? CancellationError() : APIError.transport(urlError)
        }
        return APIError.transport(URLError(.unknown))
    }
}
