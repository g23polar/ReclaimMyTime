import Foundation

/// `Block`. Rendered as-is; the client never computes or adjusts spans.
struct Block: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let planId: UUID
    let projectId: UUID
    /// Stable across plan versions; the diff key.
    let lineageId: UUID
    let span: Span
    let state: BlockState
    let gcalStatus: GcalSync
    let gcalEventId: String?

    private enum CodingKeys: String, CodingKey {
        case id, span, state
        case planId = "plan_id"
        case projectId = "project_id"
        case lineageId = "lineage_id"
        case gcalStatus = "gcal_status"
        case gcalEventId = "gcal_event_id"
    }
}

/// `WeekPlan`: plan row + its blocks ordered by `span.start`.
struct WeekPlan: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let horizon: Span
    let weekStart: LocalDate
    let timezone: String
    let version: Int
    let status: PlanStatus
    let infeasibility: Infeasibility?
    let createdAt: Date
    let supersededAt: Date?
    let blocks: [Block]

    private enum CodingKeys: String, CodingKey {
        case id, horizon, timezone, version, status, infeasibility, blocks
        case weekStart = "week_start"
        case createdAt = "created_at"
        case supersededAt = "superseded_at"
    }
}

/// `PlanDiff`: one entry per lineage present in either plan (D18).
struct PlanDiff: Codable, Hashable, Sendable {
    let planId: UUID
    /// Null on the first week (every entry is `added`).
    let againstPlanId: UUID?
    let entries: [DiffEntry]

    private enum CodingKeys: String, CodingKey {
        case entries
        case planId = "plan_id"
        case againstPlanId = "against_plan_id"
    }
}

/// `DiffEntry.change` with the spans each case requires (oneOf on `change`).
enum DiffChange: Hashable, Sendable {
    case added(after: Span)
    case removed(before: Span)
    /// Span changed (start and/or end; includes resize).
    case moved(before: Span, after: Span)
    case unchanged(before: Span, after: Span)

    var before: Span? {
        switch self {
        case .added: nil
        case .removed(let before), .moved(let before, _), .unchanged(let before, _): before
        }
    }

    var after: Span? {
        switch self {
        case .removed: nil
        case .added(let after), .moved(_, let after), .unchanged(_, let after): after
        }
    }
}

struct DiffEntry: Codable, Hashable, Sendable {
    let lineageId: UUID
    let projectId: UUID
    let change: DiffChange

    private enum CodingKeys: String, CodingKey {
        case before, after, change
        case lineageId = "lineage_id"
        case projectId = "project_id"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        lineageId = try container.decode(UUID.self, forKey: .lineageId)
        projectId = try container.decode(UUID.self, forKey: .projectId)
        let before = try container.decodeIfPresent(Span.self, forKey: .before)
        let after = try container.decodeIfPresent(Span.self, forKey: .after)
        let raw = try container.decode(String.self, forKey: .change)
        switch (raw, before, after) {
        case ("added", nil, let after?): change = .added(after: after)
        case ("removed", let before?, nil): change = .removed(before: before)
        case ("moved", let before?, let after?): change = .moved(before: before, after: after)
        case ("unchanged", let before?, let after?): change = .unchanged(before: before, after: after)
        default:
            throw DecodingError.dataCorruptedError(
                forKey: .change, in: container,
                debugDescription: "DiffEntry change '\(raw)' inconsistent with before/after"
            )
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(lineageId, forKey: .lineageId)
        try container.encode(projectId, forKey: .projectId)
        try container.encode(change.before, forKey: .before)
        try container.encode(change.after, forKey: .after)
        let raw = switch change {
        case .added: "added"
        case .removed: "removed"
        case .moved: "moved"
        case .unchanged: "unchanged"
        }
        try container.encode(raw, forKey: .change)
    }
}
