import Foundation

enum TranscriptRole: String, Codable, Hashable, Sendable {
    case user, assistant
}

/// `TranscriptMessage`. For assistant turns `content` is the previous `done.reply` verbatim and
/// `changesetId` names the changeset that turn produced.
struct TranscriptMessage: Codable, Hashable, Sendable {
    var role: TranscriptRole
    var content: String
    var changesetId: UUID?

    init(role: TranscriptRole, content: String, changesetId: UUID? = nil) {
        self.role = role
        self.content = content
        self.changesetId = changesetId
    }

    private enum CodingKeys: String, CodingKey {
        case role, content
        case changesetId = "changeset_id"
    }

    /// UUIDs go out in canonical lowercase (Foundation's default is uppercase).
    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(role, forKey: .role)
        try container.encode(content, forKey: .content)
        try container.encodeIfPresent(changesetId?.pathComponent, forKey: .changesetId)
    }
}

/// `AgentMessageRequest`: the full client-held transcript (server is stateless). Last must be `user`.
struct AgentMessageRequest: Codable, Hashable, Sendable {
    var messages: [TranscriptMessage]
}

enum AgentPhase: String, Codable, Hashable, Sendable {
    /// A stage_* tool is running.
    case staging
    /// propose_schedule is running the solver.
    case solving
}

enum AgentOutcome: String, Codable, Hashable, Sendable {
    case completed, refused
}

/// Terminal `done` payload. `reply` is authoritative: replace streamed delta text with it.
struct AgentDone: Codable, Hashable, Sendable {
    let outcome: AgentOutcome
    let reply: String
    /// Always nil when `outcome == .refused`.
    let changeset: Changeset?

    private enum CodingKeys: String, CodingKey { case outcome, reply, changeset }

    init(outcome: AgentOutcome, reply: String, changeset: Changeset?) {
        self.outcome = outcome
        self.reply = reply
        self.changeset = changeset
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        outcome = try container.decode(AgentOutcome.self, forKey: .outcome)
        reply = try container.decode(String.self, forKey: .reply)
        changeset = try container.decodeIfPresent(Changeset.self, forKey: .changeset)
        if outcome == .refused, changeset != nil {
            throw DecodingError.dataCorruptedError(forKey: .changeset, in: container, debugDescription: "a refusal carries no changeset")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(outcome, forKey: .outcome)
        try container.encode(reply, forKey: .reply)
        try container.encode(changeset, forKey: .changeset)
    }
}

/// `AgentStreamEvent`: one parsed SSE frame of `POST /agent/message`.
enum AgentEvent: Hashable, Sendable {
    case delta(text: String)
    case status(phase: AgentPhase)
    case done(AgentDone)
    case error(ErrorBody)

    /// Exactly one terminal event ends every stream.
    var isTerminal: Bool {
        switch self {
        case .done, .error: true
        case .delta, .status: false
        }
    }

    var eventName: String {
        switch self {
        case .delta: "delta"
        case .status: "status"
        case .done: "done"
        case .error: "error"
        }
    }

    private struct DeltaData: Codable { let text: String }
    private struct StatusData: Codable { let phase: AgentPhase }
    private struct ErrorData: Codable { let error: ErrorBody }

    /// Decodes a frame's `data:` JSON given its `event:` name. Returns nil for unknown event names
    /// (ignored for forward compatibility; only `done`/`error` can end a stream).
    static func decode(eventName: String, data: Data, decoder: JSONDecoder) throws -> AgentEvent? {
        switch eventName {
        case "delta": return .delta(text: try decoder.decode(DeltaData.self, from: data).text)
        case "status": return .status(phase: try decoder.decode(StatusData.self, from: data).phase)
        case "done": return .done(try decoder.decode(AgentDone.self, from: data))
        case "error": return .error(try decoder.decode(ErrorData.self, from: data).error)
        default: return nil
        }
    }

    /// Encodes the `data:` payload (used by the mock transport and tests to synthesize frames).
    func encodedData(encoder: JSONEncoder) throws -> Data {
        switch self {
        case .delta(let text): try encoder.encode(DeltaData(text: text))
        case .status(let phase): try encoder.encode(StatusData(phase: phase))
        case .done(let done): try encoder.encode(done)
        case .error(let body): try encoder.encode(ErrorData(error: body))
        }
    }
}

/// The parsed-frame shape `{event, data}` used by the fixtures (`AgentStreamEvent` schema).
extension AgentEvent: Decodable {
    private enum FrameKeys: String, CodingKey { case event, data }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: FrameKeys.self)
        let name = try container.decode(String.self, forKey: .event)
        switch name {
        case "delta": self = .delta(text: try container.decode(DeltaData.self, forKey: .data).text)
        case "status": self = .status(phase: try container.decode(StatusData.self, forKey: .data).phase)
        case "done": self = .done(try container.decode(AgentDone.self, forKey: .data))
        case "error": self = .error(try container.decode(ErrorData.self, forKey: .data).error)
        default:
            throw DecodingError.dataCorruptedError(forKey: .event, in: container, debugDescription: "Unknown agent event \(name)")
        }
    }
}
