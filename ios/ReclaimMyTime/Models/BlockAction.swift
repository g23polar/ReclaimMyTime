import Foundation

/// `BlockAction`: body of `PATCH /blocks/{id}` (oneOf on `action`).
/// `edit` carries the one absolute span a client ever sends (D7); the span comes from the user's
/// drag, not from client-side scheduling.
enum BlockAction: Codable, Hashable, Sendable {
    case complete
    case skip
    case edit(span: Span)
    case delete

    private enum CodingKeys: String, CodingKey { case action, span }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let action = try container.decode(String.self, forKey: .action)
        switch action {
        case "complete": self = .complete
        case "skip": self = .skip
        case "edit": self = .edit(span: try container.decode(Span.self, forKey: .span))
        case "delete": self = .delete
        default:
            throw DecodingError.dataCorruptedError(forKey: .action, in: container, debugDescription: "Unknown block action \(action)")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .complete: try container.encode("complete", forKey: .action)
        case .skip: try container.encode("skip", forKey: .action)
        case .edit(let span):
            try container.encode("edit", forKey: .action)
            try container.encode(span, forKey: .span)
        case .delete: try container.encode("delete", forKey: .action)
        }
    }

    var actionName: String {
        switch self {
        case .complete: "complete"
        case .skip: "skip"
        case .edit: "edit"
        case .delete: "delete"
        }
    }
}

/// `PATCH /blocks/{id}` returns 200 + Block for complete/skip and 202 + Changeset for edit/delete.
enum PatchBlockResult: Hashable, Sendable {
    case applied(Block)
    case staged(Changeset)
}
