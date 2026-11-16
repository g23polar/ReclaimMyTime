import Foundation
import Observation
import OSLog

/// The one pending changeset the user can confirm (contracts/NOTES.md "Staging model": at most one
/// per user; a newer one expires the older). Held in memory; after a relaunch `POST /gcal/sync`
/// returns the pending changeset and `AppModel` stages it here (O2, resolved by D25).
///
/// Every staged write (agent turn, PATCH, PUT, …) funnels its 202 body through `stage(_:)`.
/// Confirm/reject are the only writes this store makes; nothing is applied client-side.
@MainActor
@Observable
final class PendingChangesetStore {
    /// What became of a changeset this client has seen.
    enum Lifecycle: Hashable, Sendable {
        case pending
        case confirmed
        case rejected
        /// Replaced by a newer pending changeset (D4), or unknown to this store.
        case superseded
        /// The server reported 409 `changeset_not_pending`.
        case outOfDate
    }

    /// Transient, user-facing notices (toast).
    enum Notice: Hashable, Sendable, Identifiable {
        case outOfDate

        var id: Self { self }

        var message: String {
            switch self {
            case .outOfDate: "That proposal is out of date: your week changed since. Nothing was applied."
            }
        }
    }

    private(set) var pending: Changeset?
    private(set) var isResolving = false
    private(set) var notice: Notice?
    /// Inline error for a confirm/reject that failed for a reason other than 409.
    private(set) var actionError: String?
    /// Bumped whenever live state changed or may have changed (confirm, 409), so plan readers
    /// (T5.3 Week) know to refetch.
    private(set) var liveStateRevision = 0
    /// Bumped whenever `stage` adopts or refreshes a changeset. A sync captures it at request start
    /// so an empty answer only retires a proposal it actually saw (`retireIfUnchanged`).
    private(set) var stagingSequence = 0

    static let staleMessage = "Your week changed since this was proposed, so nothing was applied. Fetching an updated proposal…"

    /// Called after a successful confirm with the confirmed changeset.
    var onConfirmed: (@MainActor (Changeset) -> Void)?
    /// Called when a changeset is discarded (rejected, superseded, out of date); the second
    /// argument is its replacement, if any.
    var onDiscarded: (@MainActor (Changeset, Changeset?) -> Void)?
    /// Confirm answered 409 `changeset_stale`: the server staged a fresh proposal
    /// (`details.changeset_id`) that only `POST /gcal/sync` can fetch. Its 202 arrives via `stage`.
    var onStale: (@MainActor (Changeset) -> Void)?

    private var outcomes: [UUID: Lifecycle] = [:]
    /// High-water mark of adopted `created_at`s; survives resolution so ordering holds after it.
    private var newestCreatedAt: Date?
    /// Bumped by `reset()` (sign-out). A confirm/reject that resumes in a later generation
    /// publishes nothing: results from a previous session never reach the next one.
    private var generation = 0
    private let api: any APIClient
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "changeset")

    init(api: any APIClient) {
        self.api = api
    }

    var hasPending: Bool { pending != nil }

    /// Adopts a freshly staged changeset. Returns false (and ignores it) unless it is `pending`,
    /// unseen-as-resolved, and not older than the newest changeset this store has adopted, even
    /// after that one resolved: the server expired every older proposal when it created the newer
    /// one (D4), so a late response for an older write must never become actionable.
    @discardableResult
    func stage(_ changeset: Changeset) -> Bool {
        guard changeset.status == .pending else { return false }
        if let current = pending, current.id == changeset.id {
            stagingSequence += 1
            pending = changeset
            // A stale-confirm recovery that found this same changeset still pending is done.
            if actionError == Self.staleMessage { actionError = nil }
            return true
        }
        if outcomes[changeset.id] != nil {
            logger.notice("Ignoring a changeset this client already resolved")
            return false
        }
        if let newest = newestCreatedAt, changeset.createdAt < newest {
            logger.notice("Ignoring an older pending changeset")
            return false
        }
        newestCreatedAt = changeset.createdAt
        stagingSequence += 1
        if let current = pending {
            outcomes[current.id] = .superseded
            pending = changeset
            onDiscarded?(current, changeset)
        } else {
            pending = changeset
        }
        actionError = nil
        return true
    }

    func lifecycle(of id: UUID) -> Lifecycle {
        if pending?.id == id { return .pending }
        return outcomes[id] ?? .superseded
    }

    /// Confirms the pending changeset if its id matches. Returns true on success.
    @discardableResult
    func confirm(id: UUID) async -> Bool {
        guard let current = pending, current.id == id, !isResolving else { return false }
        isResolving = true
        actionError = nil
        let started = generation
        defer { if started == generation { isResolving = false } }
        do {
            let result = try await api.confirmChangeset(id: id)
            guard started == generation else { return false }
            // The server echoes the same changeset; prefer it, but only if it is the one we sent.
            let confirmed = result.id == id ? result : current
            outcomes[id] = .confirmed
            if pending?.id == id { pending = nil }
            liveStateRevision += 1
            onConfirmed?(confirmed)
            return true
        } catch {
            guard started == generation else { return false }
            handleResolutionError(error, failed: current, verb: "confirm")
            return false
        }
    }

    /// "Keep my week": rejects the pending changeset if its id matches.
    @discardableResult
    func reject(id: UUID) async -> Bool {
        guard let current = pending, current.id == id, !isResolving else { return false }
        isResolving = true
        actionError = nil
        let started = generation
        defer { if started == generation { isResolving = false } }
        do {
            _ = try await api.rejectChangeset(id: id)
            guard started == generation else { return false }
            outcomes[id] = .rejected
            if pending?.id == id { pending = nil }
            onDiscarded?(current, nil)
            return true
        } catch {
            guard started == generation else { return false }
            handleResolutionError(error, failed: current, verb: "reject")
            return false
        }
    }

    /// `POST /gcal/sync` answered `pending_changeset: null`: the server holds no pending changeset,
    /// so the local one (if nothing was staged since `sequence`) was resolved or expired elsewhere.
    /// Clears it and the banner, and resets the `created_at` high-water mark, since the server's
    /// ordering no longer constrains what comes next. Left alone while a confirm/reject is in
    /// flight: that answer settles it.
    func retireIfUnchanged(since sequence: Int) {
        guard let current = pending, stagingSequence == sequence, !isResolving else { return }
        logger.notice("Retiring a changeset the server no longer holds")
        outcomes[current.id] = .outOfDate
        pending = nil
        newestCreatedAt = nil
        actionError = nil
        liveStateRevision += 1
        onDiscarded?(current, nil)
    }

    func dismissNotice() {
        notice = nil
    }

    /// Sign-out: forget everything.
    func reset() {
        generation += 1
        pending = nil
        outcomes.removeAll()
        newestCreatedAt = nil
        notice = nil
        actionError = nil
        isResolving = false
    }

    /// 409 `changeset_not_pending` (diff-and-infeasibility.md §4): clear the banner, ask readers to
    /// refetch, toast. Anything else keeps the proposal and shows an inline retryable error.
    ///
    /// `failed` is the changeset the operation was sent for. If a newer one replaced it while the
    /// request was in flight, the supersession was already reported (`onDiscarded(failed, newer)`)
    /// and the replacement is what the user is reviewing: record the outcome and touch nothing else.
    private func handleResolutionError(_ error: any Error, failed: Changeset, verb: String) {
        let stillCurrent = pending?.id == failed.id
        if let apiError = error as? APIError, apiError.serverCode == KnownErrorCode.changesetNotPending {
            logger.notice("Changeset no longer pending on \(verb, privacy: .public)")
            guard stillCurrent else {
                outcomes[failed.id] = .outOfDate
                return
            }
            outcomes[failed.id] = .outOfDate
            pending = nil
            liveStateRevision += 1
            notice = .outOfDate
            onDiscarded?(failed, nil)
            return
        }
        if error is CancellationError || !stillCurrent { return }
        if let gcal = GcalSolveFailure(error) {
            // Fails closed (nothing applied); the client hook raised the reconnect banner.
            logger.notice("Changeset \(verb, privacy: .public) refused by Google Calendar")
            actionError = gcal.message
            return
        }
        if (error as? APIError)?.serverCode == KnownErrorCode.changesetStale {
            // Kept on screen until the fresh proposal replaces it (stage clears this message).
            logger.notice("Changeset stale on \(verb, privacy: .public)")
            actionError = Self.staleMessage
            onStale?(failed)
            return
        }
        logger.error("Changeset \(verb, privacy: .public) failed")
        actionError = "Couldn't reach the server. Nothing changed; try again."
    }
}

/// The banner's one-line summary (timetable.md §6): the reason headline for drift/google_edit,
/// otherwise the diff counts.
enum ProposalSummary {
    struct Text: Hashable, Sendable {
        let summary: String
        let doesNotFit: Bool
    }

    static func make(_ changeset: Changeset) -> Text {
        // Server-initiated proposals lead with why (design/notifications.md §4; D25).
        if let headline = ChangesetReasonCopy.headline(changeset.reason) {
            return Text(summary: headline, doesNotFit: changeset.plan?.infeasibility != nil)
        }
        guard changeset.planId != nil, let diff = changeset.diff else {
            return Text(summary: "Ready to confirm your projects", doesNotFit: false)
        }
        var moved = 0, added = 0, removed = 0
        for entry in diff.entries {
            switch entry.change {
            case .moved: moved += 1
            case .added: added += 1
            case .removed: removed += 1
            case .unchanged: break
            }
        }
        let parts = [
            moved > 0 ? "\(moved) \(moved == 1 ? "block" : "blocks") moved" : nil,
            added > 0 ? "\(added) added" : nil,
            removed > 0 ? "\(removed) removed" : nil,
        ].compactMap { $0 }
        let summary = parts.isEmpty ? "No changes to your blocks" : parts.joined(separator: " · ")
        return Text(summary: summary, doesNotFit: changeset.plan?.infeasibility != nil)
    }
}
