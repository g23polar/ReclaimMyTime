import Foundation

/// `Infeasibility`: the plan carrying it is the best partial plan. Relaxations are candidates for
/// the user to pick; picking one is a new staged write, never automatic (PRD Principle 2).
struct Infeasibility: Codable, Hashable, Sendable {
    let violations: [Violation]
    let relaxations: [Relaxation]
}

struct DemandShortfall: Codable, Hashable, Sendable {
    let projectId: UUID
    let requiredMinutes: Int
    let scheduledMinutes: Int
    let shortfallMinutes: Int

    private enum CodingKeys: String, CodingKey {
        case projectId = "project_id"
        case requiredMinutes = "required_minutes"
        case scheduledMinutes = "scheduled_minutes"
        case shortfallMinutes = "shortfall_minutes"
    }
}

/// `PinInvalidated.conflicts_with` (9 values, D17 widened per O6).
enum PinConflict: String, Codable, Hashable, Sendable, CaseIterable {
    case busy, exception, window, buffer, overlap, forbid, horizon
    case beforeNow = "before_now"
    case preferredWindow = "preferred_window"
}

struct PinInvalidated: Codable, Hashable, Sendable {
    let pinId: UUID
    let pinKind: PinKind
    let projectId: UUID?
    let span: Span
    let conflictsWith: PinConflict
    /// Set when `conflictsWith == .exception`.
    let exceptionId: UUID?
    /// The other block(s) involved for `overlap`/`buffer`; empty otherwise.
    let relatedLineageIds: [UUID]

    private enum CodingKeys: String, CodingKey {
        case span
        case pinId = "pin_id"
        case pinKind = "pin_kind"
        case projectId = "project_id"
        case conflictsWith = "conflicts_with"
        case exceptionId = "exception_id"
        case relatedLineageIds = "related_lineage_ids"
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(pinId, forKey: .pinId)
        try container.encode(pinKind, forKey: .pinKind)
        try container.encode(projectId, forKey: .projectId)
        try container.encode(span, forKey: .span)
        try container.encode(conflictsWith, forKey: .conflictsWith)
        try container.encode(exceptionId, forKey: .exceptionId)
        try container.encode(relatedLineageIds, forKey: .relatedLineageIds)
    }
}

private enum KindKey: String, CodingKey { case kind }

/// `Violation` (oneOf on `kind`).
enum Violation: Codable, Hashable, Sendable {
    case demandShortfall(DemandShortfall)
    case pinInvalidated(PinInvalidated)

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: KindKey.self)
        let kind = try container.decode(String.self, forKey: .kind)
        switch kind {
        case "demand_shortfall": self = .demandShortfall(try DemandShortfall(from: decoder))
        case "pin_invalidated": self = .pinInvalidated(try PinInvalidated(from: decoder))
        default:
            throw DecodingError.dataCorruptedError(forKey: .kind, in: container, debugDescription: "Unknown violation kind \(kind)")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: KindKey.self)
        switch self {
        case .demandShortfall(let value):
            try container.encode("demand_shortfall", forKey: .kind)
            try value.encode(to: encoder)
        case .pinInvalidated(let value):
            try container.encode("pin_invalidated", forKey: .kind)
            try value.encode(to: encoder)
        }
    }
}

/// `Relaxation` (oneOf on `kind`). Proposed values are in the project's own units (D17): hours
/// with one decimal, local `target_date`, so a pick maps 1:1 onto a `PATCH /projects` body.
enum Relaxation: Codable, Hashable, Sendable {
    case extendDeadline(projectId: UUID, targetDate: LocalDate, recoversMinutes: Int)
    case reduceEstimate(projectId: UUID, estimatedTotalHours: Double, recoversMinutes: Int)
    case reduceWeeklyTarget(projectId: UUID, weeklyTargetHours: Double, recoversMinutes: Int)
    case raiseMaxBlockMinutes(projectId: UUID, maxBlockMinutes: Int, recoversMinutes: Int)
    case pauseProject(projectId: UUID, recoversMinutes: Int)
    case removePin(pinId: UUID, recoversMinutes: Int)
    case removeException(exceptionId: UUID, recoversMinutes: Int)

    var recoversMinutes: Int {
        switch self {
        case .extendDeadline(_, _, let minutes), .reduceEstimate(_, _, let minutes),
             .reduceWeeklyTarget(_, _, let minutes), .raiseMaxBlockMinutes(_, _, let minutes),
             .pauseProject(_, let minutes), .removePin(_, let minutes), .removeException(_, let minutes):
            minutes
        }
    }

    private enum CodingKeys: String, CodingKey {
        case kind
        case recoversMinutes = "recovers_minutes"
        case projectId = "project_id"
        case targetDate = "target_date"
        case estimatedTotalHours = "estimated_total_hours"
        case weeklyTargetHours = "weekly_target_hours"
        case maxBlockMinutes = "max_block_minutes"
        case pinId = "pin_id"
        case exceptionId = "exception_id"
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let kind = try c.decode(String.self, forKey: .kind)
        let minutes = try c.decode(Int.self, forKey: .recoversMinutes)
        switch kind {
        case "extend_deadline":
            self = .extendDeadline(
                projectId: try c.decode(UUID.self, forKey: .projectId),
                targetDate: try c.decode(LocalDate.self, forKey: .targetDate),
                recoversMinutes: minutes)
        case "reduce_estimate":
            self = .reduceEstimate(
                projectId: try c.decode(UUID.self, forKey: .projectId),
                estimatedTotalHours: try c.decode(Double.self, forKey: .estimatedTotalHours),
                recoversMinutes: minutes)
        case "reduce_weekly_target":
            self = .reduceWeeklyTarget(
                projectId: try c.decode(UUID.self, forKey: .projectId),
                weeklyTargetHours: try c.decode(Double.self, forKey: .weeklyTargetHours),
                recoversMinutes: minutes)
        case "raise_max_block_minutes":
            self = .raiseMaxBlockMinutes(
                projectId: try c.decode(UUID.self, forKey: .projectId),
                maxBlockMinutes: try c.decode(Int.self, forKey: .maxBlockMinutes),
                recoversMinutes: minutes)
        case "pause_project":
            self = .pauseProject(projectId: try c.decode(UUID.self, forKey: .projectId), recoversMinutes: minutes)
        case "remove_pin":
            self = .removePin(pinId: try c.decode(UUID.self, forKey: .pinId), recoversMinutes: minutes)
        case "remove_exception":
            self = .removeException(exceptionId: try c.decode(UUID.self, forKey: .exceptionId), recoversMinutes: minutes)
        default:
            throw DecodingError.dataCorruptedError(forKey: .kind, in: c, debugDescription: "Unknown relaxation kind \(kind)")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(recoversMinutes, forKey: .recoversMinutes)
        switch self {
        case let .extendDeadline(projectId, targetDate, _):
            try c.encode("extend_deadline", forKey: .kind)
            try c.encode(projectId, forKey: .projectId)
            try c.encode(targetDate, forKey: .targetDate)
        case let .reduceEstimate(projectId, hours, _):
            try c.encode("reduce_estimate", forKey: .kind)
            try c.encode(projectId, forKey: .projectId)
            try c.encode(hours, forKey: .estimatedTotalHours)
        case let .reduceWeeklyTarget(projectId, hours, _):
            try c.encode("reduce_weekly_target", forKey: .kind)
            try c.encode(projectId, forKey: .projectId)
            try c.encode(hours, forKey: .weeklyTargetHours)
        case let .raiseMaxBlockMinutes(projectId, minutes, _):
            try c.encode("raise_max_block_minutes", forKey: .kind)
            try c.encode(projectId, forKey: .projectId)
            try c.encode(minutes, forKey: .maxBlockMinutes)
        case let .pauseProject(projectId, _):
            try c.encode("pause_project", forKey: .kind)
            try c.encode(projectId, forKey: .projectId)
        case let .removePin(pinId, _):
            try c.encode("remove_pin", forKey: .kind)
            try c.encode(pinId, forKey: .pinId)
        case let .removeException(exceptionId, _):
            try c.encode("remove_exception", forKey: .kind)
            try c.encode(exceptionId, forKey: .exceptionId)
        }
    }
}
