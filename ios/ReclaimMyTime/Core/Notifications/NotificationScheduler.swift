import Foundation
import Observation
import OSLog

/// Owns the app's local notifications (design/notifications.md): the rolling 48-hour window,
/// the 10-minute warning setting, permission state and the post-confirm pre-prompt.
///
/// `rebuild(_:)` is idempotent: identifiers are keyed by `lineage_id` and kind, so a rebuild
/// replaces what is pending rather than adding to it, and stale ones are removed. Rebuilds run one
/// at a time in call order, and only the newest matters: a rebuild superseded before it starts is
/// skipped, and one superseded mid-way stops (undoing an add that landed after it was superseded).
/// Sign-out (`signOut()`) supersedes everything queued and cleans up at once.
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
    /// Bumped by every enqueue and by sign-out; a rebuild runs only while it holds the newest.
    private var generation = 0
    private var cleanup: Task<Void, Never>?
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
        generation += 1
        let mine = generation
        let previous = queue
        queue = Task { [weak self] in
            await previous?.value
            await self?.perform(snapshot, generation: mine)
        }
    }

    /// Rebuild with the most recent snapshot (foreground with no fresh plan).
    func rebuildLast() async {
        await rebuild(lastSnapshot)
    }

    /// Waits for queued rebuilds (tests, background task).
    func waitForIdle() async {
        await cleanup?.value
        await queue?.value
    }

    private func perform(_ snapshot: NotificationSnapshot, generation mine: Int) async {
        guard mine == generation else { return }
        authorization = await center.authorization()
        guard mine == generation else { return }
        let planner = BlockNotificationPlanner(now: now(), warningsEnabled: warningsEnabled, calendar: calendar)
        let desired = authorization == .authorized ? planner.requests(for: snapshot) : []
        let desiredIds = Set(desired.map(\.identifier))
        let stale = await center.pendingIdentifiers().filter { BlockNotificationPlanner.isBlockNotification($0) && !desiredIds.contains($0) }
        guard mine == generation else { return }
        if !stale.isEmpty { await center.removePending(identifiers: stale) }
        for request in desired {
            guard mine == generation else { return }
            do {
                try await center.add(request)
            } catch {
                logger.error("Couldn't schedule a block notification")
            }
            if mine != generation {
                // Superseded while the add was in flight (sign-out cleanup may already have run):
                // take it back. A newer rebuild runs after this one and re-adds it if still wanted.
                await center.removePending(identifiers: [request.identifier])
                return
            }
        }
        scheduledIdentifiers = desired.map(\.identifier)
    }

    /// Sign-out: supersedes every queued rebuild and, without waiting for them, removes this app's
    /// pending and delivered notifications (they carry the account's projects and times).
    func signOut() {
        generation += 1
        lastSnapshot = .empty
        showsPrePrompt = false
        scheduledIdentifiers = []
        let center = self.center
        cleanup = Task {
            let pending = await center.pendingIdentifiers().filter(BlockNotificationPlanner.isAccountNotification)
            if !pending.isEmpty { await center.removePending(identifiers: pending) }
            let delivered = await center.deliveredIdentifiers().filter(BlockNotificationPlanner.isAccountNotification)
            if !delivered.isEmpty { await center.removeDelivered(identifiers: delivered) }
        }
    }

    /// Sign-out, awaited.
    func clear() async {
        signOut()
        await cleanup?.value
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
