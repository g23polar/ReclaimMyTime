import Foundation

/// Names and spans the trade-off copy needs, gathered from data the client already holds
/// (`GET /projects`, `GET /pins`, `GET /exceptions`, the proposal's own staged mutations and plan).
struct TradeOffContext: Sendable {
    struct PinInfo: Hashable, Sendable {
        let projectId: UUID?
        let span: Span
    }

    var projectNames: [UUID: String] = [:]
    var pins: [UUID: PinInfo] = [:]
    var exceptionReasons: [UUID: String] = [:]
    var exceptionSpans: [UUID: Span] = [:]
    /// lineage → project, for `related_lineage_ids`.
    var lineageProjects: [UUID: UUID] = [:]
}

struct ViolationCopy: Hashable, Sendable {
    let title: String
    let explanation: String
}

struct RelaxationCopy: Hashable, Sendable {
    let title: String
    let detail: String
}

/// Plain-language trade-off copy (diff-and-infeasibility.md §5). Amber, no guilt, in hours.
struct TradeOffCopy {
    static let pinKeptNote = "Your pin is kept on record until you remove or move it."
    static let tryItConfirmation = "This will create a new proposal. Nothing changes until you confirm it."

    var time: TimeText
    var context: TradeOffContext

    private func project(_ id: UUID?) -> String? {
        id.flatMap { context.projectNames[$0] }
    }

    // MARK: violations

    func violation(_ violation: Violation) -> ViolationCopy {
        switch violation {
        case .demandShortfall(let shortfall):
            let name = project(shortfall.projectId) ?? "A project"
            return ViolationCopy(
                title: "\(name): \(TimeText.duration(minutes: shortfall.shortfallMinutes)) short",
                explanation: "Needs \(TimeText.duration(minutes: shortfall.requiredMinutes)) this week; \(TimeText.duration(minutes: shortfall.scheduledMinutes)) fits."
            )
        case .pinInvalidated(let pin):
            let subject = pinSubject(pin)
            let (predicate, explanation): (String, String) = switch pin.conflictsWith {
            case .busy:
                ("clashes with a meeting", "Your calendar comes first, so this block can't stay there.")
            case .exception:
                ("is inside time you marked unavailable", "You said you're unavailable then, which takes priority over a pinned block.")
            case .window:
                ("is outside your schedulable hours", "It's outside the hours you gave me.")
            case .preferredWindow:
                ("is outside \(project(pin.projectId) ?? "this project")'s set times", "This project only runs at certain times.")
            case .overlap, .buffer:
                ("is too close to another block", "It overlaps or leaves no breathing room before \(otherProject(pin)).")
            case .forbid:
                ("is in time you kept free", "You removed a block from this slot earlier.")
            case .horizon:
                ("is outside this week", "It falls outside the week being planned.")
            case .beforeNow:
                ("is in the past", "That time has already gone.")
            }
            return ViolationCopy(title: "\(subject) \(predicate)", explanation: "\(explanation) \(Self.pinKeptNote)")
        }
    }

    /// "Your Thu 6:00 PM Portfolio site block"; a forbid reads as the slot that was kept free.
    private func pinSubject(_ pin: PinInvalidated) -> String {
        let slot = time.slot(pin.span)
        switch pin.pinKind {
        case .pin:
            if let name = project(pin.projectId) { return "Your \(slot) \(name) block" }
            return "Your \(slot) block"
        case .forbid:
            return "The \(slot) slot you kept free"
        }
    }

    private func otherProject(_ pin: PinInvalidated) -> String {
        let names = pin.relatedLineageIds.compactMap { context.lineageProjects[$0] }.compactMap { project($0) }
        var unique: [String] = []
        for name in names where !unique.contains(name) { unique.append(name) }
        return unique.isEmpty ? "another block" : unique.joined(separator: " and ")
    }

    // MARK: relaxations

    /// Sorted by `recovers_minutes`, largest first; ties keep server order.
    static func sorted(_ relaxations: [Relaxation]) -> [Relaxation] {
        relaxations.enumerated()
            .sorted { lhs, rhs in
                lhs.element.recoversMinutes != rhs.element.recoversMinutes
                    ? lhs.element.recoversMinutes > rhs.element.recoversMinutes
                    : lhs.offset < rhs.offset
            }
            .map(\.element)
    }

    func relaxation(_ relaxation: Relaxation) -> RelaxationCopy {
        let frees = "Frees up \(TimeText.duration(minutes: relaxation.recoversMinutes))"
        switch relaxation {
        case let .extendDeadline(projectId, targetDate, _):
            return RelaxationCopy(title: "Move \(project(projectId) ?? "this project")'s deadline to \(time.localDate(targetDate))", detail: frees)
        case let .reduceEstimate(projectId, hours, _):
            return RelaxationCopy(title: "Plan \(project(projectId) ?? "this project") as \(time.hours(hours)) h total", detail: frees)
        case let .reduceWeeklyTarget(projectId, hours, _):
            return RelaxationCopy(title: "\(project(projectId) ?? "This project"): \(time.hours(hours)) h a week", detail: frees)
        case let .raiseMaxBlockMinutes(projectId, minutes, recovers):
            return RelaxationCopy(
                title: "Let \(project(projectId) ?? "this project") run up to \(TimeText.duration(minutes: minutes))",
                detail: "Fits \(TimeText.duration(minutes: recovers)) more"
            )
        case let .pauseProject(projectId, _):
            return RelaxationCopy(title: "Pause \(project(projectId) ?? "this project") for now", detail: frees)
        case let .removePin(pinId, _):
            guard let pin = context.pins[pinId] else { return RelaxationCopy(title: "Unpin that block", detail: frees) }
            let name = project(pin.projectId).map { " \($0)" } ?? ""
            return RelaxationCopy(title: "Unpin your \(time.slot(pin.span))\(name) block", detail: frees)
        case let .removeException(exceptionId, _):
            if let reason = context.exceptionReasons[exceptionId], !reason.isEmpty {
                return RelaxationCopy(title: "Drop the '\(reason)' unavailability", detail: frees)
            }
            if let span = context.exceptionSpans[exceptionId] {
                return RelaxationCopy(title: "Drop the '\(time.slot(span))' unavailability", detail: frees)
            }
            return RelaxationCopy(title: "Drop that unavailability", detail: frees)
        }
    }

    // MARK: strip

    /// The review screen's entry line: "6 h 45 m doesn't fit this week".
    func stripText(_ infeasibility: Infeasibility) -> String {
        let shortfall = infeasibility.violations.reduce(0) { total, violation in
            if case .demandShortfall(let value) = violation { return total + value.shortfallMinutes }
            return total
        }
        if shortfall > 0 { return "\(TimeText.duration(minutes: shortfall)) doesn't fit this week" }
        let pins = infeasibility.violations.count
        return pins == 1 ? "1 pinned block can't stay where it is" : "\(pins) pinned blocks can't stay where they are"
    }
}

/// The staged write a relaxation maps onto (NOTES "Infeasibility": a pick is a new staged write,
/// never applied automatically). Values are the relaxation's own, in project units (D17).
enum RelaxationRequest: Hashable, Sendable {
    case patchProject(id: UUID, patch: ProjectPatch)
    case deletePin(id: UUID)
    case deleteException(id: UUID)

    init(_ relaxation: Relaxation) {
        switch relaxation {
        case let .extendDeadline(projectId, targetDate, _):
            self = .patchProject(id: projectId, patch: ProjectPatch(targetDate: targetDate))
        case let .reduceEstimate(projectId, hours, _):
            self = .patchProject(id: projectId, patch: ProjectPatch(estimatedTotalHours: hours))
        case let .reduceWeeklyTarget(projectId, hours, _):
            self = .patchProject(id: projectId, patch: ProjectPatch(weeklyTargetHours: hours))
        case let .raiseMaxBlockMinutes(projectId, minutes, _):
            self = .patchProject(id: projectId, patch: ProjectPatch(maxBlockMinutes: minutes))
        case let .pauseProject(projectId, _):
            self = .patchProject(id: projectId, patch: ProjectPatch(status: .paused))
        case let .removePin(pinId, _):
            self = .deletePin(id: pinId)
        case let .removeException(exceptionId, _):
            self = .deleteException(id: exceptionId)
        }
    }

    /// Sends the write; the 202 body is the new pending changeset.
    func send(_ api: any APIClient) async throws -> Changeset {
        switch self {
        case let .patchProject(id, patch):
            // A relaxation never edits only `name`, so the server stages it; a direct apply here
            // would mean the contract changed under us.
            switch try await api.updateProject(id: id, patch: patch) {
            case .staged(let changeset): changeset
            case .applied: throw APIError.unexpectedStatus(200)
            }
        case let .deletePin(id): try await api.deletePin(id: id)
        case let .deleteException(id): try await api.deleteException(id: id)
        }
    }
}
