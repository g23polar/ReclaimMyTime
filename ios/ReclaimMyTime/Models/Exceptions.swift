import Foundation

/// Mirrors the DDL CHECK `blocks_all OR cardinality(blocks_tags) >= 1`.
private func exceptionCheckHolds(blocksAll: Bool, blocksTags: [String]) -> Bool {
    blocksAll || !blocksTags.isEmpty
}

/// `AvailabilityException`. `span` is absolute.
struct AvailabilityException: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let span: Span
    let blocksAll: Bool
    let blocksTags: [String]
    let reason: String?
    let createdAt: Date

    private enum CodingKeys: String, CodingKey {
        case id, span, reason
        case blocksAll = "blocks_all"
        case blocksTags = "blocks_tags"
        case createdAt = "created_at"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        span = try container.decode(Span.self, forKey: .span)
        blocksAll = try container.decode(Bool.self, forKey: .blocksAll)
        blocksTags = try container.decode([String].self, forKey: .blocksTags)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        createdAt = try container.decode(Date.self, forKey: .createdAt)
        guard exceptionCheckHolds(blocksAll: blocksAll, blocksTags: blocksTags) else {
            throw DecodingError.dataCorruptedError(
                forKey: .blocksTags, in: container,
                debugDescription: "exception needs blocks_all=true or >=1 blocks_tags"
            )
        }
    }
}

struct AvailabilityExceptionList: Codable, Hashable, Sendable {
    let exceptions: [AvailabilityException]
}

/// `ExceptionDraft`: local dates/times only; the server resolves them with X-Timezone (D10).
struct ExceptionDraft: Codable, Hashable, Sendable {
    var startDate: LocalDate
    var startTime: LocalTime?
    var endDate: LocalDate
    var endTime: LocalTime?
    var blocksAll: Bool?
    var blocksTags: [String]?
    var reason: String?

    private enum CodingKeys: String, CodingKey {
        case reason
        case startDate = "start_date"
        case startTime = "start_time"
        case endDate = "end_date"
        case endTime = "end_time"
        case blocksAll = "blocks_all"
        case blocksTags = "blocks_tags"
    }

    /// Returns nil when neither `blocksAll` nor a non-empty `blocksTags` is given (DB CHECK).
    init?(
        startDate: LocalDate,
        startTime: LocalTime? = nil,
        endDate: LocalDate,
        endTime: LocalTime? = nil,
        blocksAll: Bool? = nil,
        blocksTags: [String]? = nil,
        reason: String? = nil
    ) {
        guard exceptionCheckHolds(blocksAll: blocksAll ?? false, blocksTags: blocksTags ?? []) else { return nil }
        self.startDate = startDate
        self.startTime = startTime
        self.endDate = endDate
        self.endTime = endTime
        self.blocksAll = blocksAll
        self.blocksTags = blocksTags
        self.reason = reason
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        startDate = try container.decode(LocalDate.self, forKey: .startDate)
        startTime = try container.decodeIfPresent(LocalTime.self, forKey: .startTime)
        endDate = try container.decode(LocalDate.self, forKey: .endDate)
        endTime = try container.decodeIfPresent(LocalTime.self, forKey: .endTime)
        blocksAll = try container.decodeIfPresent(Bool.self, forKey: .blocksAll)
        blocksTags = try container.decodeIfPresent([String].self, forKey: .blocksTags)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        guard exceptionCheckHolds(blocksAll: blocksAll ?? false, blocksTags: blocksTags ?? []) else {
            throw DecodingError.dataCorruptedError(
                forKey: .blocksTags, in: container,
                debugDescription: "exception needs blocks_all=true or >=1 blocks_tags"
            )
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(startDate, forKey: .startDate)
        try container.encodeIfPresent(startTime, forKey: .startTime)
        try container.encode(endDate, forKey: .endDate)
        try container.encodeIfPresent(endTime, forKey: .endTime)
        try container.encodeIfPresent(blocksAll, forKey: .blocksAll)
        try container.encodeIfPresent(blocksTags, forKey: .blocksTags)
        try container.encodeIfPresent(reason, forKey: .reason)
    }
}

/// Payload of a staged `exception_added`: server-resolved, id pre-assigned, span absolute.
struct StagedExceptionPayload: Codable, Hashable, Sendable {
    let exceptionId: UUID
    let span: Span
    let blocksAll: Bool
    let blocksTags: [String]
    let reason: String?

    private enum CodingKeys: String, CodingKey {
        case span, reason
        case exceptionId = "exception_id"
        case blocksAll = "blocks_all"
        case blocksTags = "blocks_tags"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        exceptionId = try container.decode(UUID.self, forKey: .exceptionId)
        span = try container.decode(Span.self, forKey: .span)
        blocksAll = try container.decode(Bool.self, forKey: .blocksAll)
        blocksTags = try container.decode([String].self, forKey: .blocksTags)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        guard exceptionCheckHolds(blocksAll: blocksAll, blocksTags: blocksTags) else {
            throw DecodingError.dataCorruptedError(
                forKey: .blocksTags, in: container,
                debugDescription: "exception needs blocks_all=true or >=1 blocks_tags"
            )
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(exceptionId, forKey: .exceptionId)
        try container.encode(span, forKey: .span)
        try container.encode(blocksAll, forKey: .blocksAll)
        try container.encode(blocksTags, forKey: .blocksTags)
        try container.encode(reason, forKey: .reason)
    }
}
