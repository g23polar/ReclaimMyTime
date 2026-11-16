import Foundation

// Enums mirrored verbatim from contracts/openapi.yaml ("enums (verbatim DDL)").

enum ConnectionStatus: String, Codable, Hashable, Sendable {
    case active, revoked
}

enum ProjectKind: String, Codable, Hashable, Sendable {
    case deadline, perennial
}

/// `staged` = inserted by a pending `project_created` mutation; never returned by list/get (D23).
enum ProjectStatus: String, Codable, Hashable, Sendable {
    case staged, active, paused, done, archived
}

/// The subset of `ProjectStatus` accepted by `PATCH /projects/{id}` (archive is DELETE).
enum PatchableProjectStatus: String, Codable, Hashable, Sendable {
    case active, paused, done
}

enum DayDiversity: String, Codable, Hashable, Sendable {
    case focused, mixed
}

enum PinKind: String, Codable, Hashable, Sendable {
    case pin, forbid
}

enum PlanStatus: String, Codable, Hashable, Sendable {
    case proposed, live, superseded, rejected
}

enum ChangesetStatus: String, Codable, Hashable, Sendable {
    case pending, confirmed, rejected, expired
}

/// Why a changeset was proposed (D25). Strict while the contract is frozen: an unknown value fails
/// decoding like every other enum here (NOTES suggests treating unknown values as `user_edit`; revisit
/// if the contract unfreezes).
enum ChangesetReason: String, Codable, Hashable, Sendable {
    /// A REST staged write (projects, preferences, exceptions, pins, block edit/delete).
    case userEdit = "user_edit"
    /// An agent turn (`POST /agent/message`).
    case agent
    /// Calendar busy time now overlaps live blocks; re-solved with no constraint change.
    case drift
    /// One of our events was edited inside Google Calendar; `staged` carries the resulting pin(s).
    case googleEdit = "google_edit"

    /// Server-initiated re-plans may carry no constraint change (D25; google_edit since T4.3b).
    var allowsEmptyStaged: Bool { self == .drift || self == .googleEdit }
}

enum BlockState: String, Codable, Hashable, Sendable {
    case planned, completed, skipped
}

enum GcalSync: String, Codable, Hashable, Sendable {
    case pending, synced, failed
}

enum MutationSource: String, Codable, Hashable, Sendable {
    case agent, manual, system
}

enum MutationKind: String, Codable, Hashable, Sendable {
    case projectCreated = "project_created"
    case projectUpdated = "project_updated"
    case projectArchived = "project_archived"
    case preferencesUpdated = "preferences_updated"
    case exceptionAdded = "exception_added"
    case exceptionRemoved = "exception_removed"
    case pinAdded = "pin_added"
    case pinRemoved = "pin_removed"
    case planProposed = "plan_proposed"
    case planConfirmed = "plan_confirmed"
    case planRejected = "plan_rejected"
}
