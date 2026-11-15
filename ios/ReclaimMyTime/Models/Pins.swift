import Foundation

/// `BlockPin`. `projectId` is null only for a blanket forbid.
struct BlockPin: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let projectId: UUID?
    let span: Span
    let kind: PinKind
    let createdFromBlockId: UUID?
    let createdAt: Date

    private enum CodingKeys: String, CodingKey {
        case id, span, kind
        case projectId = "project_id"
        case createdFromBlockId = "created_from_block_id"
        case createdAt = "created_at"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        projectId = try container.decodeIfPresent(UUID.self, forKey: .projectId)
        span = try container.decode(Span.self, forKey: .span)
        kind = try container.decode(PinKind.self, forKey: .kind)
        createdFromBlockId = try container.decodeIfPresent(UUID.self, forKey: .createdFromBlockId)
        createdAt = try container.decode(Date.self, forKey: .createdAt)
        if kind == .pin, projectId == nil {
            throw DecodingError.dataCorruptedError(forKey: .projectId, in: container, debugDescription: "a pin requires project_id")
        }
    }
}

struct PinList: Codable, Hashable, Sendable {
    let pins: [BlockPin]
}
