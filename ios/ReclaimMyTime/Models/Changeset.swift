import Foundation

/// `Changeset`: staged mutations + proposed plan + diff. `planId`, `plan` and `diff` are all set
/// or all nil (nil during onboarding before preferences exist). Nothing is applied until confirm.
/// `staged` is empty only for a `drift` re-solve (D25, O11) or a `google_edit` whose Google-side
/// move was already applied, leaving only the knock-on re-plan (T4.3b); decoding rejects an empty
/// list for any other reason, mirroring the schema's `if`/`then`.
struct Changeset: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let status: ChangesetStatus
    let reason: ChangesetReason
    let planId: UUID?
    let staged: [StagedMutation]
    let createdAt: Date
    let resolvedAt: Date?
    let plan: WeekPlan?
    let diff: PlanDiff?
    /// Template text for manual/system proposals; nil for agent proposals.
    let narration: String?

    private enum CodingKeys: String, CodingKey {
        case id, status, reason, staged, plan, diff, narration
        case planId = "plan_id"
        case createdAt = "created_at"
        case resolvedAt = "resolved_at"
    }

    init(
        id: UUID,
        status: ChangesetStatus,
        reason: ChangesetReason,
        planId: UUID?,
        staged: [StagedMutation],
        createdAt: Date,
        resolvedAt: Date?,
        plan: WeekPlan?,
        diff: PlanDiff?,
        narration: String?
    ) {
        self.id = id
        self.status = status
        self.reason = reason
        self.planId = planId
        self.staged = staged
        self.createdAt = createdAt
        self.resolvedAt = resolvedAt
        self.plan = plan
        self.diff = diff
        self.narration = narration
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        status = try container.decode(ChangesetStatus.self, forKey: .status)
        reason = try container.decode(ChangesetReason.self, forKey: .reason)
        planId = try container.decodeIfPresent(UUID.self, forKey: .planId)
        staged = try container.decode([StagedMutation].self, forKey: .staged)
        createdAt = try container.decode(Date.self, forKey: .createdAt)
        resolvedAt = try container.decodeIfPresent(Date.self, forKey: .resolvedAt)
        plan = try container.decodeIfPresent(WeekPlan.self, forKey: .plan)
        diff = try container.decodeIfPresent(PlanDiff.self, forKey: .diff)
        narration = try container.decodeIfPresent(String.self, forKey: .narration)
        if staged.isEmpty, !reason.allowsEmptyStaged {
            throw DecodingError.dataCorruptedError(forKey: .staged, in: container, debugDescription: "staged may be empty only when reason is drift or google_edit")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(status, forKey: .status)
        try container.encode(reason, forKey: .reason)
        try container.encode(planId, forKey: .planId)
        try container.encode(staged, forKey: .staged)
        try container.encode(createdAt, forKey: .createdAt)
        try container.encode(resolvedAt, forKey: .resolvedAt)
        try container.encode(plan, forKey: .plan)
        try container.encode(diff, forKey: .diff)
        try container.encode(narration, forKey: .narration)
    }
}

/// Payload of a staged `pin_added`: the block_pins row to insert.
struct StagedPinPayload: Codable, Hashable, Sendable {
    let pinId: UUID
    let projectId: UUID?
    let span: Span
    let kind: PinKind
    let createdFromBlockId: UUID?

    private enum CodingKeys: String, CodingKey {
        case span, kind
        case pinId = "pin_id"
        case projectId = "project_id"
        case createdFromBlockId = "created_from_block_id"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pinId = try container.decode(UUID.self, forKey: .pinId)
        projectId = try container.decodeIfPresent(UUID.self, forKey: .projectId)
        span = try container.decode(Span.self, forKey: .span)
        kind = try container.decode(PinKind.self, forKey: .kind)
        createdFromBlockId = try container.decodeIfPresent(UUID.self, forKey: .createdFromBlockId)
        if kind == .pin, projectId == nil {
            throw DecodingError.dataCorruptedError(forKey: .projectId, in: container, debugDescription: "a pin requires project_id")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(pinId, forKey: .pinId)
        try container.encode(projectId, forKey: .projectId)
        try container.encode(span, forKey: .span)
        try container.encode(kind, forKey: .kind)
        try container.encode(createdFromBlockId, forKey: .createdFromBlockId)
    }
}

/// The kind-specific part of a `StagedMutation` (oneOf on `kind`; constraint-changing subset only).
enum StagedChange: Hashable, Sendable {
    case projectCreated(projectId: UUID, project: ProjectDraft)
    case projectUpdated(projectId: UUID, changes: ProjectPatch)
    case projectArchived(projectId: UUID)
    case preferencesUpdated(UserPreferences)
    case exceptionAdded(StagedExceptionPayload)
    case exceptionRemoved(exceptionId: UUID)
    case pinAdded(StagedPinPayload)
    case pinRemoved(pinId: UUID)

    var kind: MutationKind {
        switch self {
        case .projectCreated: .projectCreated
        case .projectUpdated: .projectUpdated
        case .projectArchived: .projectArchived
        case .preferencesUpdated: .preferencesUpdated
        case .exceptionAdded: .exceptionAdded
        case .exceptionRemoved: .exceptionRemoved
        case .pinAdded: .pinAdded
        case .pinRemoved: .pinRemoved
        }
    }
}

/// `StagedMutation`: one element of `Changeset.staged`.
struct StagedMutation: Codable, Hashable, Sendable {
    let source: MutationSource
    let interpretation: String?
    let change: StagedChange

    var kind: MutationKind { change.kind }

    private enum CodingKeys: String, CodingKey { case kind, source, interpretation, payload }

    private enum PayloadKeys: String, CodingKey {
        case project, changes, preferences
        case projectId = "project_id"
        case exceptionId = "exception_id"
        case pinId = "pin_id"
    }

    init(source: MutationSource, interpretation: String?, change: StagedChange) {
        self.source = source
        self.interpretation = interpretation
        self.change = change
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        source = try container.decode(MutationSource.self, forKey: .source)
        interpretation = try container.decodeIfPresent(String.self, forKey: .interpretation)
        let kind = try container.decode(MutationKind.self, forKey: .kind)
        switch kind {
        case .exceptionAdded:
            change = .exceptionAdded(try container.decode(StagedExceptionPayload.self, forKey: .payload))
        case .pinAdded:
            change = .pinAdded(try container.decode(StagedPinPayload.self, forKey: .payload))
        case .projectCreated, .projectUpdated, .projectArchived, .preferencesUpdated,
             .exceptionRemoved, .pinRemoved:
            let payload = try container.nestedContainer(keyedBy: PayloadKeys.self, forKey: .payload)
            switch kind {
            case .projectCreated:
                change = .projectCreated(
                    projectId: try payload.decode(UUID.self, forKey: .projectId),
                    project: try payload.decode(ProjectDraft.self, forKey: .project))
            case .projectUpdated:
                change = .projectUpdated(
                    projectId: try payload.decode(UUID.self, forKey: .projectId),
                    changes: try payload.decode(ProjectPatch.self, forKey: .changes))
            case .projectArchived:
                change = .projectArchived(projectId: try payload.decode(UUID.self, forKey: .projectId))
            case .preferencesUpdated:
                change = .preferencesUpdated(try payload.decode(UserPreferences.self, forKey: .preferences))
            case .exceptionRemoved:
                change = .exceptionRemoved(exceptionId: try payload.decode(UUID.self, forKey: .exceptionId))
            default:
                change = .pinRemoved(pinId: try payload.decode(UUID.self, forKey: .pinId))
            }
        case .planProposed, .planConfirmed, .planRejected:
            // plan_* are server trace rows, never staged.
            throw DecodingError.dataCorruptedError(forKey: .kind, in: container, debugDescription: "\(kind.rawValue) is not a stageable mutation")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(change.kind, forKey: .kind)
        try container.encode(source, forKey: .source)
        try container.encode(interpretation, forKey: .interpretation)
        switch change {
        case .exceptionAdded(let payload):
            try container.encode(payload, forKey: .payload)
        case .pinAdded(let payload):
            try container.encode(payload, forKey: .payload)
        case let .projectCreated(projectId, project):
            var payload = container.nestedContainer(keyedBy: PayloadKeys.self, forKey: .payload)
            try payload.encode(projectId, forKey: .projectId)
            try payload.encode(project, forKey: .project)
        case let .projectUpdated(projectId, changes):
            var payload = container.nestedContainer(keyedBy: PayloadKeys.self, forKey: .payload)
            try payload.encode(projectId, forKey: .projectId)
            try payload.encode(changes, forKey: .changes)
        case .projectArchived(let projectId):
            var payload = container.nestedContainer(keyedBy: PayloadKeys.self, forKey: .payload)
            try payload.encode(projectId, forKey: .projectId)
        case .preferencesUpdated(let preferences):
            var payload = container.nestedContainer(keyedBy: PayloadKeys.self, forKey: .payload)
            try payload.encode(preferences, forKey: .preferences)
        case .exceptionRemoved(let exceptionId):
            var payload = container.nestedContainer(keyedBy: PayloadKeys.self, forKey: .payload)
            try payload.encode(exceptionId, forKey: .exceptionId)
        case .pinRemoved(let pinId):
            var payload = container.nestedContainer(keyedBy: PayloadKeys.self, forKey: .payload)
            try payload.encode(pinId, forKey: .pinId)
        }
    }
}
