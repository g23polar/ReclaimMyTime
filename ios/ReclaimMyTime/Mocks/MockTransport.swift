import Foundation
import OSLog

/// Serves contracts/fixtures as an `HTTPTransport` (enabled by `-UseMockAPI`, see `AppConfig`).
///
/// Routing is by (method, path template, variant):
/// - the request's method + path is matched against each fixture's template (`{param}` = one segment);
/// - the variant is the per-operation override if set (`setVariant`), else for `patchBlock` the
///   variant matching the body's `action`, else `defaultVariants`, else the alphabetically first
///   2xx/3xx case. Selection is deterministic.
///
/// The mock is stateless: it never mutates or computes anything (no scheduling), it only replays.
actor MockTransport: HTTPTransport {
    /// Deterministic defaults for operations with several success fixtures; chosen to walk the
    /// fixture story (NOTES.md "Story").
    static let defaultVariants: [String: String] = [
        "getGcalStatus": "connected",
        "getProject": "deadline",
        "createProject": "perennial_onboarding",
        "createException": "traveling_friday",
        "postAgentMessage": "laptop",
        "signInWithApple": "new_user",
        "getMe": "ok",
        "getCurrentPlan": "ok",
        "getPreferences": "ok",
        "confirmChangeset": "ok",
        "updateProject": "weekly_target",
        "deletePin": "forbid",
        "getPlanDiff": "laptop",
    ]

    /// `PATCH /blocks/{id}` response depends on the action, not just the route.
    static let patchBlockVariantByAction: [String: String] = [
        "complete": "complete",
        "skip": "skip",
        "edit": "edit_pin",
        "delete": "delete_forbid",
    ]

    private let fixturesByOperation: [String: [Fixture]]
    private let routes: [(method: String, segments: [String], operation: String)]
    private var overrides: [String: String] = [:]
    private let frameDelay: Duration
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "mock")

    init(fixtures: [Fixture], frameDelay: Duration = .milliseconds(120)) {
        var byOperation: [String: [Fixture]] = [:]
        var routes: [(String, [String], String)] = []
        for fixture in fixtures {
            if byOperation[fixture.operation] == nil {
                routes.append((fixture.method, Self.segments(fixture.path), fixture.operation))
            }
            byOperation[fixture.operation, default: []].append(fixture)
        }
        self.fixturesByOperation = byOperation.mapValues { $0.sorted { $0.caseName < $1.caseName } }
        // Literal segments win over `{param}` templates when both match.
        self.routes = routes.sorted { lhs, rhs in
            lhs.1.filter { $0.hasPrefix("{") }.count < rhs.1.filter { $0.hasPrefix("{") }.count
        }
        self.frameDelay = frameDelay
    }

    /// Loads the fixtures bundled with the app. Missing fixtures yield an empty mock (every route 404s).
    static func bundled(frameDelay: Duration = .milliseconds(120)) -> MockTransport {
        let fixtures = FixtureLoader.bundledDirectory().flatMap { try? FixtureLoader.loadMockFixtures(from: $0) } ?? []
        return MockTransport(fixtures: fixtures, frameDelay: frameDelay)
    }

    // MARK: test / debug hooks

    /// Forces a fixture `case` for an operationId (e.g. `("getCurrentPlan", "no_live_plan")`).
    func setVariant(_ caseName: String?, for operation: String) {
        overrides[operation] = caseName
    }

    func resetVariants() {
        overrides.removeAll()
    }

    var operations: [String] { fixturesByOperation.keys.sorted() }

    /// The fixture a request would be served from (nil if unrouted).
    func resolve(_ request: URLRequest) -> Fixture? {
        guard let operation = operation(for: request) else { return nil }
        guard let candidates = fixturesByOperation[operation], !candidates.isEmpty else { return nil }

        let wanted: String?
        if let override = overrides[operation] {
            wanted = override
        } else if operation == "patchBlock", let action = Self.blockAction(in: request) {
            wanted = Self.patchBlockVariantByAction[action]
        } else {
            wanted = Self.defaultVariants[operation]
        }
        if let wanted, let match = candidates.first(where: { $0.caseName == wanted }) {
            return match
        }
        return candidates.first(where: { $0.status < 400 }) ?? candidates.first
    }

    // MARK: HTTPTransport

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (fixture, response) = try respond(to: request)
        guard let fixture else { return (Self.notFoundBody(request), response) }
        return (fixture.responseBody ?? Data(), response)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        let (fixture, response) = try respond(to: request)
        guard let fixture else {
            let body = Self.notFoundBody(request)
            return (response, AsyncThrowingStream { $0.yield(body); $0.finish() })
        }
        guard fixture.isEventStream else {
            let body = fixture.responseBody ?? Data()
            return (response, AsyncThrowingStream { $0.yield(body); $0.finish() })
        }
        // Replay frames as real SSE bytes with small delays; a heartbeat comment leads, and each
        // frame is split into two chunks so the client's parser always sees a chunk boundary.
        let frames = fixture.streamEvents.map { event -> Data in
            var data = Data("event: \(event.event)\ndata: ".utf8)
            data.append(event.data)
            data.append(Data("\n\n".utf8))
            return data
        }
        let delay = frameDelay
        let stream = AsyncThrowingStream<Data, any Error> { continuation in
            let task = Task {
                do {
                    continuation.yield(Data(": ping\n\n".utf8))
                    for frame in frames {
                        if delay > .zero { try await Task.sleep(for: delay) }
                        let mid = frame.count / 2
                        continuation.yield(frame.prefix(mid))
                        continuation.yield(frame.suffix(from: frame.startIndex + mid))
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
        return (response, stream)
    }

    // MARK: routing

    private func respond(to request: URLRequest) throws -> (Fixture?, HTTPURLResponse) {
        guard let url = request.url else { throw URLError(.badURL) }
        guard let fixture = resolve(request) else {
            logger.notice("No fixture for \(request.httpMethod ?? "?", privacy: .public) \(url.path(), privacy: .public)")
            return (nil, try Self.httpResponse(url: url, status: 404, headers: ["Content-Type": "application/json"]))
        }
        var headers = fixture.responseHeaders
        if fixture.responseBody != nil, headers["Content-Type"] == nil {
            headers["Content-Type"] = fixture.isEventStream ? "text/event-stream" : "application/json"
        }
        return (fixture, try Self.httpResponse(url: url, status: fixture.status, headers: headers))
    }

    private func operation(for request: URLRequest) -> String? {
        guard let url = request.url else { return nil }
        let method = (request.httpMethod ?? "GET").uppercased()
        let segments = Self.segments(url.path())
        return routes.first { route in
            route.method == method && route.segments.count == segments.count
                && zip(route.segments, segments).allSatisfy { template, actual in
                    template.hasPrefix("{") || template == actual
                }
        }?.operation
    }

    private static func segments(_ path: String) -> [String] {
        path.split(separator: "/").map(String.init)
    }

    private static func blockAction(in request: URLRequest) -> String? {
        guard let body = request.httpBody ?? request.httpBodyStream.map(readAll),
              let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
        else { return nil }
        return object["action"] as? String
    }

    private static func readAll(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            guard read > 0 else { break }
            data.append(buffer, count: read)
        }
        return data
    }

    private static func httpResponse(url: URL, status: Int, headers: [String: String]) throws -> HTTPURLResponse {
        guard let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers) else {
            throw URLError(.badServerResponse)
        }
        return response
    }

    /// Mirrors the contract's 404 `not_found` envelope for unrouted paths.
    private static func notFoundBody(_ request: URLRequest) -> Data {
        let envelope = ErrorEnvelope(error: ErrorBody(
            code: KnownErrorCode.notFound,
            message: "No mock fixture for \(request.httpMethod ?? "GET") \(request.url?.path() ?? "")"
        ))
        return (try? JSONCoding.makeEncoder().encode(envelope)) ?? Data()
    }
}
