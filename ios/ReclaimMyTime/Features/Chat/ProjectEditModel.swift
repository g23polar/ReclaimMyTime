import Foundation
import Observation

/// The Edit sheet for a project summary card (onboarding.md §4): a Form prefilled from the staged
/// values. Saving sends `PATCH /projects/{id}` with only the changed fields; the 202 changeset
/// supersedes the old one.
@MainActor
@Observable
final class ProjectEditModel {
    let original: ProjectCard
    var name: String
    var targetDate: Date
    var estimatedTotalHours: Double
    var weeklyTargetHours: Double
    var maxBlockMinutes: Int
    var hasMinimum: Bool
    var minBlockMinutes: Int
    var requirements: [String]
    var newTag = ""
    private(set) var isSaving = false
    private(set) var errorMessage: String?

    /// The calendar the picker presents in (device calendar by default). Only its time zone is
    /// used for wire conversion; `LocalDate` is always Gregorian (`wireCalendar`).
    private let calendar: Calendar
    /// What the form opened with. A field is sent only if the user moved it away from this, so a
    /// UI fallback for a value the card doesn't know (partial `project_updated` cards) is never
    /// mistaken for an edit.
    private let initial: Snapshot

    private struct Snapshot {
        let name: String
        let targetDate: Date
        let estimatedTotalHours: Double
        let weeklyTargetHours: Double
        let maxBlockMinutes: Int
        let hasMinimum: Bool
        let minBlockMinutes: Int
        let requirements: [String]
    }

    init(card: ProjectCard, calendar: Calendar = .current) {
        let start = Snapshot(
            name: card.name ?? "",
            targetDate: card.targetDate.flatMap { Self.date(from: $0, calendar: calendar) } ?? Date(),
            estimatedTotalHours: card.estimatedTotalHours ?? 1,
            weeklyTargetHours: card.weeklyTargetHours ?? 1,
            maxBlockMinutes: card.maxBlockMinutes ?? 60,
            hasMinimum: card.minBlockMinutes != nil,
            minBlockMinutes: card.minBlockMinutes ?? 30,
            requirements: card.requirements
        )
        original = card
        self.calendar = calendar
        initial = start
        name = start.name
        targetDate = start.targetDate
        estimatedTotalHours = start.estimatedTotalHours
        weeklyTargetHours = start.weeklyTargetHours
        maxBlockMinutes = start.maxBlockMinutes
        hasMinimum = start.hasMinimum
        minBlockMinutes = start.minBlockMinutes
        requirements = start.requirements
    }

    var kind: ProjectKind? { original.kind }

    var trimmedName: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }

    var canSave: Bool { !isSaving && !trimmedName.isEmpty && (!hasMinimum || minBlockMinutes <= maxBlockMinutes) }

    func addTag() {
        let tag = newTag.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        newTag = ""
        guard !tag.isEmpty, !requirements.contains(tag) else { return }
        requirements.append(tag)
    }

    func removeTag(_ tag: String) {
        requirements.removeAll { $0 == tag }
    }

    /// Only the fields that differ from the staged values; nil when nothing changed. Hours are
    /// rounded to the contract's one decimal (D19).
    func makePatch() -> ProjectPatch? {
        var patch = ProjectPatch()
        if trimmedName != initial.name.trimmingCharacters(in: .whitespacesAndNewlines) { patch.name = trimmedName }
        switch original.kind {
        case .deadline:
            let date = Self.localDate(from: targetDate, calendar: calendar)
            if let date, date != Self.localDate(from: initial.targetDate, calendar: calendar) { patch.targetDate = date }
            let hours = Self.oneDecimal(estimatedTotalHours)
            if hours != Self.oneDecimal(initial.estimatedTotalHours) { patch.estimatedTotalHours = hours }
        case .perennial:
            let hours = Self.oneDecimal(weeklyTargetHours)
            if hours != Self.oneDecimal(initial.weeklyTargetHours) { patch.weeklyTargetHours = hours }
        case nil:
            break
        }
        if maxBlockMinutes != initial.maxBlockMinutes { patch.maxBlockMinutes = maxBlockMinutes }
        // The contract has no way to clear min_block_minutes; switching it off is not sent.
        if hasMinimum, !initial.hasMinimum || minBlockMinutes != initial.minBlockMinutes { patch.minBlockMinutes = minBlockMinutes }
        if requirements != initial.requirements { patch.requirements = requirements }
        return patch == ProjectPatch() ? nil : patch
    }

    /// Returns the new pending changeset, or nil (with `errorMessage`) on failure or no change.
    func save(api: any APIClient) async -> Changeset? {
        guard canSave, let patch = makePatch() else { return nil }
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            return try await api.updateProject(id: original.id, patch: patch)
        } catch let error as APIError {
            switch error.serverCode {
            case KnownErrorCode.validationFailed:
                errorMessage = error.errorBody?.validationFields.first.map { "\($0.path): \($0.message)" } ?? "Those values didn't work."
            case KnownErrorCode.projectArchived:
                errorMessage = "This project was archived, so it can't be edited."
            case KnownErrorCode.notFound:
                errorMessage = "This project is no longer in your plan."
            default:
                errorMessage = "Couldn't save that. Nothing changed; try again."
            }
            return nil
        } catch {
            if !(error is CancellationError) { errorMessage = "Couldn't save that. Nothing changed; try again." }
            return nil
        }
    }

    static func oneDecimal(_ value: Double) -> Double {
        (value * 10).rounded() / 10
    }

    /// Calendar-date conversions for the DatePicker (input only). `LocalDate` is a Gregorian label
    /// (contract `format: date`), so conversion uses a fixed Gregorian calendar in the presenting
    /// calendar's time zone, whatever the device calendar (Buddhist, Japanese, ...) is.
    static func localDate(from date: Date, calendar: Calendar) -> LocalDate? {
        LocalDate.from(date, timeZone: calendar.timeZone)
    }

    static func date(from local: LocalDate, calendar: Calendar) -> Date? {
        local.middayInstant(timeZone: calendar.timeZone)
    }
}
