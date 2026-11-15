import Foundation

/// The deadline/perennial XOR (D9) as one enum, so invalid combinations are unrepresentable.
enum ProjectGoal: Hashable, Sendable {
    case deadline(targetDate: LocalDate, estimatedTotalHours: Double)
    case perennial(weeklyTargetHours: Double)

    var kind: ProjectKind {
        switch self {
        case .deadline: .deadline
        case .perennial: .perennial
        }
    }
}

private enum ProjectCodingKeys: String, CodingKey {
    case id, name, kind, status, requirements
    case targetDate = "target_date"
    case estimatedTotalHours = "estimated_total_hours"
    case weeklyTargetHours = "weekly_target_hours"
    case maxBlockMinutes = "max_block_minutes"
    case minBlockMinutes = "min_block_minutes"
    case preferredWindows = "preferred_windows"
    case preferredStrict = "preferred_strict"
    case createdAt = "created_at"
}

private extension KeyedDecodingContainer where K == ProjectCodingKeys {
    func decodeGoal() throws -> ProjectGoal {
        switch try decode(ProjectKind.self, forKey: .kind) {
        case .deadline:
            return .deadline(
                targetDate: try decode(LocalDate.self, forKey: .targetDate),
                estimatedTotalHours: try decode(Double.self, forKey: .estimatedTotalHours)
            )
        case .perennial:
            return .perennial(weeklyTargetHours: try decode(Double.self, forKey: .weeklyTargetHours))
        }
    }
}

/// `Project` (oneOf DeadlineProject | PerennialProject, discriminated on `kind`).
struct Project: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let name: String
    let goal: ProjectGoal
    let maxBlockMinutes: Int
    let minBlockMinutes: Int
    let preferredWindows: WeekWindows?
    let preferredStrict: Bool
    let requirements: [String]
    let status: ProjectStatus
    let createdAt: Date

    var kind: ProjectKind { goal.kind }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: ProjectCodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        goal = try container.decodeGoal()
        maxBlockMinutes = try container.decode(Int.self, forKey: .maxBlockMinutes)
        minBlockMinutes = try container.decode(Int.self, forKey: .minBlockMinutes)
        preferredWindows = try container.decodeIfPresent(WeekWindows.self, forKey: .preferredWindows)
        preferredStrict = try container.decode(Bool.self, forKey: .preferredStrict)
        requirements = try container.decode([String].self, forKey: .requirements)
        status = try container.decode(ProjectStatus.self, forKey: .status)
        createdAt = try container.decode(Date.self, forKey: .createdAt)
    }

    /// Response shape: the other kind's fields are present as `null` (D9).
    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: ProjectCodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(name, forKey: .name)
        try container.encode(goal.kind, forKey: .kind)
        switch goal {
        case let .deadline(targetDate, hours):
            try container.encode(targetDate, forKey: .targetDate)
            try container.encode(hours, forKey: .estimatedTotalHours)
            try container.encodeNil(forKey: .weeklyTargetHours)
        case let .perennial(hours):
            try container.encodeNil(forKey: .targetDate)
            try container.encodeNil(forKey: .estimatedTotalHours)
            try container.encode(hours, forKey: .weeklyTargetHours)
        }
        try container.encode(maxBlockMinutes, forKey: .maxBlockMinutes)
        try container.encode(minBlockMinutes, forKey: .minBlockMinutes)
        try container.encode(preferredWindows, forKey: .preferredWindows)
        try container.encode(preferredStrict, forKey: .preferredStrict)
        try container.encode(requirements, forKey: .requirements)
        try container.encode(status, forKey: .status)
        try container.encode(createdAt, forKey: .createdAt)
    }
}

/// `PATCH /projects/{id}`: a name-only edit is applied directly (200 + Project); anything else is
/// staged (202 + Changeset).
enum UpdateProjectResult: Hashable, Sendable {
    case applied(Project)
    case staged(Changeset)
}

struct ProjectList: Codable, Hashable, Sendable {
    let projects: [Project]
}

/// `ProjectDraft` (oneOf DeadlineProjectDraft | PerennialProjectDraft). Request body of
/// `POST /projects`, and the payload of a staged `project_created`. Optional fields are omitted
/// when nil; the server applies defaults.
struct ProjectDraft: Codable, Hashable, Sendable {
    var name: String
    var goal: ProjectGoal
    var maxBlockMinutes: Int
    var minBlockMinutes: Int?
    var preferredWindows: WeekWindows?
    var preferredStrict: Bool?
    var requirements: [String]?

    init(
        name: String,
        goal: ProjectGoal,
        maxBlockMinutes: Int,
        minBlockMinutes: Int? = nil,
        preferredWindows: WeekWindows? = nil,
        preferredStrict: Bool? = nil,
        requirements: [String]? = nil
    ) {
        self.name = name
        self.goal = goal
        self.maxBlockMinutes = maxBlockMinutes
        self.minBlockMinutes = minBlockMinutes
        self.preferredWindows = preferredWindows
        self.preferredStrict = preferredStrict
        self.requirements = requirements
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: ProjectCodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        goal = try container.decodeGoal()
        maxBlockMinutes = try container.decode(Int.self, forKey: .maxBlockMinutes)
        minBlockMinutes = try container.decodeIfPresent(Int.self, forKey: .minBlockMinutes)
        preferredWindows = try container.decodeIfPresent(WeekWindows.self, forKey: .preferredWindows)
        preferredStrict = try container.decodeIfPresent(Bool.self, forKey: .preferredStrict)
        requirements = try container.decodeIfPresent([String].self, forKey: .requirements)
    }

    /// Draft schemas are `additionalProperties: false`: the other kind's fields are omitted, not null.
    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: ProjectCodingKeys.self)
        try container.encode(name, forKey: .name)
        try container.encode(goal.kind, forKey: .kind)
        switch goal {
        case let .deadline(targetDate, hours):
            try container.encode(targetDate, forKey: .targetDate)
            try container.encode(hours, forKey: .estimatedTotalHours)
        case let .perennial(hours):
            try container.encode(hours, forKey: .weeklyTargetHours)
        }
        try container.encode(maxBlockMinutes, forKey: .maxBlockMinutes)
        try container.encodeIfPresent(minBlockMinutes, forKey: .minBlockMinutes)
        try container.encodeIfPresent(preferredWindows, forKey: .preferredWindows)
        try container.encodeIfPresent(preferredStrict, forKey: .preferredStrict)
        try container.encodeIfPresent(requirements, forKey: .requirements)
    }
}

/// `ProjectPatch`: partial update. nil = field absent. `preferredWindows: .null` clears the windows.
/// `kind` is immutable and archive is DELETE, so neither is representable here.
struct ProjectPatch: Codable, Hashable, Sendable {
    var name: String?
    var targetDate: LocalDate?
    var estimatedTotalHours: Double?
    var weeklyTargetHours: Double?
    var maxBlockMinutes: Int?
    var minBlockMinutes: Int?
    var preferredWindows: Nullable<WeekWindows>?
    var preferredStrict: Bool?
    var requirements: [String]?
    var status: PatchableProjectStatus?

    init(
        name: String? = nil,
        targetDate: LocalDate? = nil,
        estimatedTotalHours: Double? = nil,
        weeklyTargetHours: Double? = nil,
        maxBlockMinutes: Int? = nil,
        minBlockMinutes: Int? = nil,
        preferredWindows: Nullable<WeekWindows>? = nil,
        preferredStrict: Bool? = nil,
        requirements: [String]? = nil,
        status: PatchableProjectStatus? = nil
    ) {
        self.name = name
        self.targetDate = targetDate
        self.estimatedTotalHours = estimatedTotalHours
        self.weeklyTargetHours = weeklyTargetHours
        self.maxBlockMinutes = maxBlockMinutes
        self.minBlockMinutes = minBlockMinutes
        self.preferredWindows = preferredWindows
        self.preferredStrict = preferredStrict
        self.requirements = requirements
        self.status = status
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: ProjectCodingKeys.self)
        name = try container.decodeIfPresent(String.self, forKey: .name)
        targetDate = try container.decodeIfPresent(LocalDate.self, forKey: .targetDate)
        estimatedTotalHours = try container.decodeIfPresent(Double.self, forKey: .estimatedTotalHours)
        weeklyTargetHours = try container.decodeIfPresent(Double.self, forKey: .weeklyTargetHours)
        maxBlockMinutes = try container.decodeIfPresent(Int.self, forKey: .maxBlockMinutes)
        minBlockMinutes = try container.decodeIfPresent(Int.self, forKey: .minBlockMinutes)
        if container.contains(.preferredWindows) {
            if try container.decodeNil(forKey: .preferredWindows) {
                preferredWindows = .null
            } else {
                preferredWindows = .value(try container.decode(WeekWindows.self, forKey: .preferredWindows))
            }
        } else {
            preferredWindows = nil
        }
        preferredStrict = try container.decodeIfPresent(Bool.self, forKey: .preferredStrict)
        requirements = try container.decodeIfPresent([String].self, forKey: .requirements)
        // `status` in a patch only accepts active|paused|done.
        if let raw = try container.decodeIfPresent(String.self, forKey: .status) {
            guard let status = PatchableProjectStatus(rawValue: raw) else {
                throw DecodingError.dataCorruptedError(
                    forKey: .status, in: container,
                    debugDescription: "PATCH status must be active|paused|done, got \(raw)"
                )
            }
            self.status = status
        } else {
            status = nil
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: ProjectCodingKeys.self)
        try container.encodeIfPresent(name, forKey: .name)
        try container.encodeIfPresent(targetDate, forKey: .targetDate)
        try container.encodeIfPresent(estimatedTotalHours, forKey: .estimatedTotalHours)
        try container.encodeIfPresent(weeklyTargetHours, forKey: .weeklyTargetHours)
        try container.encodeIfPresent(maxBlockMinutes, forKey: .maxBlockMinutes)
        try container.encodeIfPresent(minBlockMinutes, forKey: .minBlockMinutes)
        switch preferredWindows {
        case .none: break
        case .some(.null): try container.encodeNil(forKey: .preferredWindows)
        case .some(.value(let windows)): try container.encode(windows, forKey: .preferredWindows)
        }
        try container.encodeIfPresent(preferredStrict, forKey: .preferredStrict)
        try container.encodeIfPresent(requirements, forKey: .requirements)
        try container.encodeIfPresent(status, forKey: .status)
    }
}
