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

    private let calendar: Calendar

    init(card: ProjectCard, calendar: Calendar = .current) {
        original = card
        self.calendar = calendar
        name = card.name ?? ""
        targetDate = card.targetDate.flatMap { Self.date(from: $0, calendar: calendar) } ?? Date()
        estimatedTotalHours = card.estimatedTotalHours ?? 1
        weeklyTargetHours = card.weeklyTargetHours ?? 1
        maxBlockMinutes = card.maxBlockMinutes ?? 60
        hasMinimum = card.minBlockMinutes != nil
        minBlockMinutes = card.minBlockMinutes ?? 30
        requirements = card.requirements
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
        if trimmedName != (original.name ?? "") { patch.name = trimmedName }
        switch original.kind {
        case .deadline:
            let date = Self.localDate(from: targetDate, calendar: calendar)
            if let date, date != original.targetDate { patch.targetDate = date }
            let hours = Self.oneDecimal(estimatedTotalHours)
            if hours != original.estimatedTotalHours.map(Self.oneDecimal) { patch.estimatedTotalHours = hours }
        case .perennial:
            let hours = Self.oneDecimal(weeklyTargetHours)
            if hours != original.weeklyTargetHours.map(Self.oneDecimal) { patch.weeklyTargetHours = hours }
        case nil:
            break
        }
        if maxBlockMinutes != original.maxBlockMinutes { patch.maxBlockMinutes = maxBlockMinutes }
        // The contract has no way to clear min_block_minutes; switching it off is not sent.
        if hasMinimum, minBlockMinutes != original.minBlockMinutes { patch.minBlockMinutes = minBlockMinutes }
        if requirements != original.requirements { patch.requirements = requirements }
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

    /// Calendar-date conversions for the DatePicker (input only).
    static func localDate(from date: Date, calendar: Calendar) -> LocalDate? {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        guard let y = parts.year, let m = parts.month, let d = parts.day else { return nil }
        return LocalDate(String(format: "%04d-%02d-%02d", y, m, d))
    }

    static func date(from local: LocalDate, calendar: Calendar) -> Date? {
        let parts = local.rawValue.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12))
    }
}
