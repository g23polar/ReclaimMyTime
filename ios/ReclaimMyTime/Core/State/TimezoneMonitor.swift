import Foundation
import Observation
import OSLog

/// The time-zone change prompt (design/notifications.md §3, PRD §10). On foreground (and when the
/// live plan changes) the device zone is compared with the zone the plan was built in
/// (`WeekPlan.timezone`, which the server sets from `preferences.timezone` at solve time).
/// "Re-plan my week" is a staged `PUT /preferences` with the new zone → proposal review.
/// "Keep it as is" suppresses the prompt for this plan-zone → device-zone pair.
@MainActor
@Observable
final class TimezoneMonitor {
    struct Prompt: Hashable, Identifiable, Sendable {
        let planZone: String
        let deviceZone: String
        var id: String { "\(planZone)>\(deviceZone)" }

        var title: String { "You're in a new time zone" }
        var message: String {
            "Your week was planned for \(Self.city(planZone)). Want me to re-plan the rest of it for \(Self.city(deviceZone))? Times already done stay as they are."
        }

        /// "America/Los_Angeles" → "Los Angeles", "Asia/Tokyo" → "Tokyo".
        static func city(_ identifier: String) -> String {
            let last = identifier.split(separator: "/").last.map(String.init) ?? identifier
            return last.replacingOccurrences(of: "_", with: " ")
        }
    }

    static let suppressedKey = "timezone.keptAsIs"

    private(set) var prompt: Prompt?
    private(set) var isReplanning = false
    private(set) var replanError: String?
    /// Set when a re-plan was staged; the sheet's `onDismiss` opens review then.
    private(set) var reviewRequested = false

    /// Device zone; injectable for tests.
    var deviceTimeZone: @MainActor () -> TimeZone = {
        // Drop Foundation's cached zone so a change while suspended is seen on foreground.
        NSTimeZone.resetSystemTimeZone()
        return TimeZone.current
    }

    private let api: any APIClient
    private let pending: PendingChangesetStore
    private let defaults: UserDefaults
    private var replanChangesetId: UUID?
    /// Bumped by `reset()` (sign-out): a replan started before it writes and publishes nothing.
    private var generation = 0
    /// The move the staged re-plan answers.
    private var replanPrompt: Prompt?
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "timezone")

    init(api: any APIClient, pending: PendingChangesetStore, defaults: UserDefaults) {
        self.api = api
        self.pending = pending
        self.defaults = defaults
    }

    /// Raises (or clears) the prompt for the live plan.
    func check(plan: WeekPlan?) {
        guard let plan else {
            prompt = nil
            return
        }
        let device = deviceTimeZone().identifier
        if device == plan.timezone {
            prompt = nil
            defaults.removeObject(forKey: Self.suppressedKey)
            return
        }
        let candidate = Prompt(planZone: plan.timezone, deviceZone: device)
        if defaults.string(forKey: Self.suppressedKey) == candidate.id { return }
        // A re-plan for this move is already waiting for review.
        if let replanChangesetId, pending.pending?.id == replanChangesetId { return }
        if prompt != candidate {
            replanError = nil
            prompt = candidate
        }
    }

    /// "Keep it as is": quiet until the zone changes again.
    func keepAsIs() {
        if let prompt { defaults.set(prompt.id, forKey: Self.suppressedKey) }
        prompt = nil
    }

    /// Swiped away without choosing: ask again next foreground.
    func dismissForNow() {
        prompt = nil
    }

    /// "Re-plan my week": `PUT /preferences` with the current preferences and the device zone. The
    /// request also carries the new zone as `X-Timezone`; the server re-solves and returns 202 + a
    /// pending changeset, which goes to review. Nothing is applied here.
    @discardableResult
    func replan(current: UserPreferences?) async -> Bool {
        guard let prompt, !isReplanning else { return false }
        let started = generation
        isReplanning = true
        replanError = nil
        defer { if started == generation { isReplanning = false } }
        do {
            var preferences: UserPreferences
            if let current {
                preferences = current
            } else {
                preferences = try await api.getPreferences()
                // Signed out meanwhile: never send the old account's preferences.
                guard started == generation else { return false }
            }
            preferences.timezone = prompt.deviceZone
            let changeset = try await api.putPreferences(preferences)
            guard started == generation, pending.stage(changeset) else { return false }
            replanChangesetId = changeset.id
            replanPrompt = prompt
            reviewRequested = true
            self.prompt = nil
            return true
        } catch {
            guard started == generation else { return false }
            logger.notice("Re-plan for a new time zone failed")
            replanError = GcalSolveFailure(error)?.message ?? "Couldn't re-plan right now. Nothing changed; try again."
            return false
        }
    }

    /// The sheet finished dismissing: true once if review should open now.
    func consumeReviewRequest() -> Bool {
        defer { reviewRequested = false }
        return reviewRequested
    }

    /// The re-plan proposal was rejected or went out of date (not replaced by a newer one): treat
    /// it as "Keep it as is" for that move.
    func changesetDiscarded(_ changeset: Changeset, replacement: Changeset?) {
        guard changeset.id == replanChangesetId else { return }
        if replacement == nil, let replanPrompt {
            defaults.set(replanPrompt.id, forKey: Self.suppressedKey)
        }
        replanChangesetId = nil
        replanPrompt = nil
    }

    func reset() {
        generation += 1
        isReplanning = false
        prompt = nil
        replanChangesetId = nil
        replanPrompt = nil
        reviewRequested = false
        replanError = nil
    }
}
