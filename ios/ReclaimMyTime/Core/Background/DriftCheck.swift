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

/// A server-proposed drift fix worth telling a user whose app is closed (design §4).
struct DriftAlert: Hashable, Sendable {
    let changesetId: UUID
    /// The first live block the new busy time overlaps.
    let projectId: UUID
    let blockStart: Date
}

/// Finds a pending drift changeset for the live plan.
protocol DriftSource: Sendable {
    func pendingDrift(livePlan: WeekPlan) async throws -> DriftAlert?
}

/// TODO(T4.3, contract gap G3/O2): there is no drift endpoint and no route that returns a pending
/// changeset, so the background check cannot see a server-proposed drift fix. When T4.3 adds one
/// (e.g. `GET /changesets?status=pending` with `source: system`), detect it here and return the
/// first clashing block. Until then this no-ops; in the foreground drift reaches the user through
/// the pending-changeset banner once the server stages it.
struct UnavailableDriftSource: DriftSource {
    func pendingDrift(livePlan: WeekPlan) async throws -> DriftAlert? {
        nil
    }
}

enum DriftNotification {
    static let prefix = "drift."
    static func identifier(_ changesetId: UUID) -> String { prefix + changesetId.pathComponent }

    static func request(_ alert: DriftAlert, projectName: String, time: TimeText) -> LocalNotificationRequest {
        let copy = NotificationCopy.drift(project: projectName, startText: time.time(alert.blockStart))
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
