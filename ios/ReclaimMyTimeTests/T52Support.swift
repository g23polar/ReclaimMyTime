import Foundation
import XCTest
@testable import ReclaimMyTime

/// Wraps the fixture mock and records every request (with body) the client sends.
actor RecordingTransport: HTTPTransport {
    let inner: MockTransport
    private(set) var requests: [URLRequest] = []

    init(inner: MockTransport) {
        self.inner = inner
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        return try await inner.data(for: request)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        requests.append(request)
        return try await inner.stream(for: request)
    }

    func requests(method: String, path: String) -> [URLRequest] {
        requests.filter { $0.httpMethod == method && $0.url?.path() == path }
    }

    func agentRequests() throws -> [AgentMessageRequest] {
        try requests(method: "POST", path: "/agent/message").map {
            try JSONDecoder().decode(AgentMessageRequest.self, from: XCTUnwrap($0.httpBody))
        }
    }
}

@MainActor
struct T52Harness {
    let model: AppModel
    let mock: MockTransport
    let transport: RecordingTransport
    let defaults: UserDefaults

    var chat: ChatModel { model.chat }
    var pending: PendingChangesetStore { model.pending }

    init(frameDelay: Duration = .zero) throws {
        mock = MockTransport(fixtures: try TestFixtures.all(), frameDelay: frameDelay)
        transport = RecordingTransport(inner: mock)
        let suite = "T52.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        let (store, _) = makeTestSessionStore()
        model = AppModel(config: AppConfig(backend: .mock), transport: transport, sessionStore: store, defaults: defaults)
    }
}

enum T52Fixtures {
    static let decoder = JSONCoding.makeDecoder()

    static func changeset(_ file: String) throws -> Changeset {
        try decoder.decode(Changeset.self, from: XCTUnwrap(TestFixtures.named(file).responseBody))
    }

    static func agentDone(_ file: String) throws -> AgentDone {
        let fixture = try TestFixtures.named(file)
        let done = try XCTUnwrap(fixture.streamEvents.last)
        return try decoder.decode(AgentDone.self, from: done.data)
    }

    static let workoutId = UUID(uuidString: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b02")!
    static let portfolioId = UUID(uuidString: "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b01")!

    /// A plan-less onboarding changeset staging the given drafts (built in tests; no fixture covers
    /// an agent turn that stages projects).
    static func staging(_ projects: [(UUID, ProjectDraft)], extra: [StagedMutation] = [], createdAt: Date = Date(timeIntervalSince1970: 1_763_000_000)) -> Changeset {
        Changeset(
            id: UUID(),
            status: .pending,
            planId: nil,
            staged: projects.map { StagedMutation(source: .agent, interpretation: nil, change: .projectCreated(projectId: $0.0, project: $0.1)) } + extra,
            createdAt: createdAt,
            resolvedAt: nil,
            plan: nil,
            diff: nil,
            narration: nil
        )
    }

    static let workoutDraft = ProjectDraft(name: "Workout", goal: .perennial(weeklyTargetHours: 4), maxBlockMinutes: 60, requirements: ["gym"])
    static let portfolioDraft = ProjectDraft(name: "Portfolio site", goal: .deadline(targetDate: LocalDate("2026-12-12")!, estimatedTotalHours: 30), maxBlockMinutes: 120, requirements: ["laptop"])
}
