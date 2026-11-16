import BackgroundTasks
import Foundation

enum BackgroundTaskID {
    /// Registered in Config/Info.plist (`BGTaskSchedulerPermittedIdentifiers`).
    static let driftCheck = "com.reclaimmytime.app.drift-check"
    /// Earliest the next refresh may run; the system decides the actual time.
    static let refreshInterval: TimeInterval = 60 * 60
}

/// The only door to `BGTaskScheduler`. Tests substitute a fake.
protocol BackgroundRefreshScheduling: Sendable {
    func submitAppRefresh(identifier: String, earliestBeginDate: Date) throws
}

struct SystemBackgroundRefreshScheduler: BackgroundRefreshScheduling {
    func submitAppRefresh(identifier: String, earliestBeginDate: Date) throws {
        let request = BGAppRefreshTaskRequest(identifier: identifier)
        request.earliestBeginDate = earliestBeginDate
        try BGTaskScheduler.shared.submit(request)
    }
}

/// A server-proposed changeset worth telling a user whose app is closed (design §4): a `drift`
/// re-solve, or a `google_edit` (one of our events was moved or deleted inside Google Calendar; D25).
struct DriftAlert: Hashable, Sendable {
    enum Kind: Hashable, Sendable {
        case drift
        /// google_edit with staged pin(s): nothing applied yet; the pinned block is named.
        case googleEdit
        /// google_edit with empty `staged`: the user's Google move/delete is already live and only
        /// the knock-on re-plan is proposed (T4.3b). No block is named: the diff's `after` spans
        /// are proposals, not the user's edit.
        case googleEditApplied
        /// google_edit whose staged mutations name no block (e.g. a forbid for a Google delete).
        case googleEditUnnamed
    }

    let changesetId: UUID
    /// drift: the first live block the new busy time pushes out. googleEdit: the pinned block.
    /// Nil for the generic google_edit kinds.
    let projectId: UUID?
    /// drift: where that block was (the clash). googleEdit: where the user moved it.
    let blockStart: Date?
    let kind: Kind

    init(changesetId: UUID, projectId: UUID, blockStart: Date, kind: Kind = .drift) {
        self.changesetId = changesetId
        self.projectId = projectId
        self.blockStart = blockStart
        self.kind = kind
    }

    private init(changesetId: UUID, generic kind: Kind) {
        self.changesetId = changesetId
        projectId = nil
        blockStart = nil
        self.kind = kind
    }

    /// Reads the alert off a pending changeset. Nil for `user_edit`/`agent` (the user just made
    /// those), for anything not pending, and for a drift with no block to name. Display only: it
    /// picks a block for the copy from what the server sent and computes nothing.
    init?(changeset: Changeset) {
        guard changeset.status == .pending else { return nil }
        switch changeset.reason {
        case .userEdit, .agent:
            return nil
        case .drift:
            // The clash is where a live block used to be: the earliest moved/removed `before`.
            let changed = (changeset.diff?.entries ?? [])
                .filter { if case .unchanged = $0.change { false } else { true } }
            guard let entry = changed
                .compactMap({ entry in entry.change.before.map { (entry.projectId, $0.start) } })
                .min(by: { $0.1 < $1.1 })
            else { return nil }
            self.init(changesetId: changeset.id, projectId: entry.0, blockStart: entry.1, kind: .drift)
        case .googleEdit:
            guard !changeset.staged.isEmpty else {
                self.init(changesetId: changeset.id, generic: .googleEditApplied)
                return
            }
            let pinned = changeset.staged.compactMap { mutation -> (UUID, Date)? in
                guard case .pinAdded(let pin) = mutation.change, pin.kind == .pin, let project = pin.projectId else { return nil }
                return (project, pin.span.start)
            }.min(by: { $0.1 < $1.1 })
            guard let pinned else {
                self.init(changesetId: changeset.id, generic: .googleEditUnnamed)
                return
            }
            self.init(changesetId: changeset.id, projectId: pinned.0, blockStart: pinned.1, kind: .googleEdit)
        }
    }
}

/// What one `POST /gcal/sync` told us: the pending changeset (any reason, so a relaunch recovers
/// the banner; O2) and, when it is a drift or google_edit proposal, the alert for it.
struct DriftCheckResult: Hashable, Sendable {
    let pendingChangeset: Changeset?
    let outbox: OutboxCounts

    var alert: DriftAlert? { pendingChangeset.flatMap(DriftAlert.init(changeset:)) }
}

/// Runs the Google sync and drift check. Errors are the client's `APIError` (409
/// `gcal_reconnect_required`, 503 `gcal_unavailable`, …), handled by the caller.
protocol DriftSource: Sendable {
    func check() async throws -> DriftCheckResult
}

/// `POST /gcal/sync` (D25): the server drains the write-back outbox, ingests Google-side edits and
/// checks for drift, then returns the user's pending changeset. Never applies anything.
struct GcalSyncDriftSource: DriftSource {
    let api: any APIClient

    func check() async throws -> DriftCheckResult {
        let result = try await api.syncGcal()
        return DriftCheckResult(pendingChangeset: result.pendingChangeset, outbox: result.outbox)
    }
}

enum DriftNotification {
    static let prefix = "drift."
    static func identifier(_ changesetId: UUID) -> String { prefix + changesetId.pathComponent }

    static func request(_ alert: DriftAlert, projectName: String, time: TimeText) -> LocalNotificationRequest {
        let startText = alert.blockStart.map(time.time) ?? ""
        let copy = switch alert.kind {
        case .drift: NotificationCopy.drift(project: projectName, startText: startText)
        case .googleEdit: NotificationCopy.googleEdit(project: projectName, startText: startText)
        case .googleEditApplied: NotificationCopy.googleEditApplied
        case .googleEditUnnamed: NotificationCopy.googleEditUnnamed
        }
        return LocalNotificationRequest(
            identifier: identifier(alert.changesetId),
            title: copy.title,
            body: copy.body,
            categoryIdentifier: NotificationCategoryID.drift,
            fireDate: nil,
            userInfo: [
                NotificationRoute.Key.kind: NotificationRoute.Kind.drift.rawValue,
                NotificationRoute.Key.changesetId: alert.changesetId.pathComponent,
            ]
        )
    }
}
