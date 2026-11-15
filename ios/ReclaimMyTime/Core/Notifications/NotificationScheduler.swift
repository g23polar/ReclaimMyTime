import Foundation
import Observation
import OSLog

/// Owns the app's local notifications (design/notifications.md): the rolling 48-hour window,
/// the 10-minute warning setting, permission state and the post-confirm pre-prompt.
///
/// `rebuild(_:)` is idempotent: identifiers are keyed by `lineage_id` and kind, so a rebuild
/// replaces what is pending rather than adding to it, and stale ones are removed. Rebuilds run one
/// at a time in call order.
@MainActor
@Observable
final class NotificationScheduler {
    static let warningsKey = "notifications.warningsEnabled"
    static let prePromptDismissedKey = "notifications.prePromptDismissed"

    /// Settings › Notifications › "Heads-up 10 minutes before" (off by default).
    var warningsEnabled: Bool {
        didSet {
            guard warningsEnabled != oldValue else { return }
            defaults.set(warningsEnabled, forKey: Self.warningsKey)
            enqueueRebuild(lastSnapshot)
        }
    }

    private(set) var authorization: NotificationAuthorization = .notDetermined
    /// The pre-prompt card ("Want a nudge when each block starts?").
    private(set) var showsPrePrompt = false
    /// Identifiers this scheduler last asked the system to hold.
    private(set) var scheduledIdentifiers: [String] = []

    /// The device clock and calendar; injectable for tests.
    var now: @MainActor () -> Date = { Date() }
    var calendar: Calendar = .autoupdatingCurrent

    private let center: any LocalNotificationCenter
    private let defaults: UserDefaults
    private var lastSnapshot = NotificationSnapshot.empty
    private var queue: Task<Void, Never>?
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "notifications")

    init(center: any LocalNotificationCenter, defaults: UserDefaults) {
        self.center = center
        self.defaults = defaults
        warningsEnabled = defaults.bool(forKey: Self.warningsKey)
    }

    var prePromptDismissed: Bool { defaults.bool(forKey: Self.prePromptDismissedKey) }

    func registerCategories() async {
        await center.registerCategories()
    }

    func refreshAuthorization() async {
        authorization = await center.authorization()
    }

    // MARK: window

    /// Re-registers the window for `snapshot` (plan change, foreground, setting change).
    func rebuild(_ snapshot: NotificationSnapshot) async {
        enqueueRebuild(snapshot)
        await queue?.value
    }

    /// Queues a rebuild behind any in progress and returns at once (synchronous callers).
    func enqueueRebuild(_ snapshot: NotificationSnapshot) {
        lastSnapshot = snapshot
        let previous = queue
        queue = Task { [weak self] in
            await previous?.value
            await self?.perform(snapshot)
        }
    }

    /// Rebuild with the most recent snapshot (foreground with no fresh plan).
    func rebuildLast() async {
        await rebuild(lastSnapshot)
    }

    /// Waits for queued rebuilds (tests, background task).
    func waitForIdle() async {
        await queue?.value
    }

    private func perform(_ snapshot: NotificationSnapshot) async {
        authorization = await center.authorization()
        let planner = BlockNotificationPlanner(now: now(), warningsEnabled: warningsEnabled, calendar: calendar)
        let desired = authorization == .authorized ? planner.requests(for: snapshot) : []
        let desiredIds = Set(desired.map(\.identifier))
        let stale = await center.pendingIdentifiers().filter { BlockNotificationPlanner.isBlockNotification($0) && !desiredIds.contains($0) }
        if !stale.isEmpty { await center.removePending(identifiers: stale) }
        for request in desired {
            do {
                try await center.add(request)
            } catch {
                logger.error("Couldn't schedule a block notification")
            }
        }
        scheduledIdentifiers = desired.map(\.identifier)
    }

    /// Sign-out: nothing of the previous account may fire.
    func clear() async {
        await rebuild(.empty)
    }

    // MARK: alerts outside the window

    /// Posts a one-off notification now (drift, §4).
    func post(_ request: LocalNotificationRequest) async {
        do {
            try await center.add(request)
        } catch {
            logger.error("Couldn't post a notification")
        }
    }

    // MARK: permission (design/notifications.md "Permission request")

    /// After a week is confirmed: offer the pre-prompt unless the user said "Not now" or the
    /// system already has an answer.
    func weekConfirmed() async {
        guard !prePromptDismissed else { return }
        await refreshAuthorization()
        if authorization == .notDetermined { showsPrePrompt = true }
    }

    /// "Turn on": the system prompt, then fill the window if allowed.
    func acceptPrePrompt() async {
        showsPrePrompt = false
        // The system asks only once, so the card has done its job either way.
        defaults.set(true, forKey: Self.prePromptDismissedKey)
        _ = await center.requestAuthorization()
        await refreshAuthorization()
        await rebuild(lastSnapshot)
    }

    /// "Not now": remembered; the card doesn't come back.
    func declinePrePrompt() {
        showsPrePrompt = false
        defaults.set(true, forKey: Self.prePromptDismissedKey)
    }

    /// Settings › "Turn on notifications" when the system hasn't asked yet.
    func requestFromSettings() async {
        _ = await center.requestAuthorization()
        await refreshAuthorization()
        await rebuild(lastSnapshot)
    }
}
