import Foundation
import Observation

/// The in-chat preferences step (onboarding.md §4 "Preferences step"): schedulable-hours windows
/// and focused vs mixed. Builds a full `UserPreferences` for `PUT /preferences`. This is input
/// capture only: windows are the user's own wall-clock times, validated for the contract's shape
/// (sorted, non-overlapping, no midnight crossing), never scheduled against.
@MainActor
@Observable
final class PreferencesFormModel {
    enum DayGroup: String, CaseIterable, Identifiable, Hashable, Sendable {
        case weekdays, weekends, everyDay
        case mon, tue, wed, thu, fri, sat, sun

        var id: Self { self }

        var label: String {
            switch self {
            case .weekdays: "Weekdays"
            case .weekends: "Weekends"
            case .everyDay: "Every day"
            case .mon: "Monday"
            case .tue: "Tuesday"
            case .wed: "Wednesday"
            case .thu: "Thursday"
            case .fri: "Friday"
            case .sat: "Saturday"
            case .sun: "Sunday"
            }
        }

        var days: [Weekday] {
            switch self {
            case .weekdays: [.mon, .tue, .wed, .thu, .fri]
            case .weekends: [.sat, .sun]
            case .everyDay: Weekday.allCases
            case .mon: [.mon]
            case .tue: [.tue]
            case .wed: [.wed]
            case .thu: [.thu]
            case .fri: [.fri]
            case .sat: [.sat]
            case .sun: [.sun]
            }
        }
    }

    struct WindowRow: Identifiable, Hashable, Sendable {
        let id: UUID
        var days: DayGroup
        /// Minutes after local midnight, 0..<1440.
        var startMinutes: Int
        /// Minutes after local midnight; 0 means midnight at the end of the day ("24:00").
        var endMinutes: Int

        init(id: UUID = UUID(), days: DayGroup, startMinutes: Int, endMinutes: Int) {
            self.id = id
            self.days = days
            self.startMinutes = startMinutes
            self.endMinutes = endMinutes
        }

        var effectiveEnd: Int { endMinutes == 0 ? 24 * 60 : endMinutes }
    }

    enum ValidationIssue: Error, Hashable, Sendable {
        case noWindows
        case endBeforeStart
        case overlap(Weekday)
        case noDiversity

        var message: String {
            switch self {
            case .noWindows: "Add at least one window."
            case .endBeforeStart: "Each window has to end after it starts."
            case .overlap(let day): "Two of your \(Self.dayName(day)) windows overlap."
            case .noDiversity: "Pick themed days or variety to continue."
            }
        }

        private static func dayName(_ day: Weekday) -> String {
            switch day {
            case .mon: "Monday"
            case .tue: "Tuesday"
            case .wed: "Wednesday"
            case .thu: "Thursday"
            case .fri: "Friday"
            case .sat: "Saturday"
            case .sun: "Sunday"
            }
        }
    }

    enum Phase: Hashable, Sendable {
        case editing
        case submitting
        case submitted
    }

    /// Not asked in the spec; the contract requires it. TODO(main session): confirm the default.
    static let defaultBufferMinutes = 15

    var rows: [WindowRow]
    var diversity: DayDiversity?
    private(set) var phase: Phase = .editing
    private(set) var submitError: String?

    init(rows: [WindowRow]? = nil, diversity: DayDiversity? = nil) {
        // The spec's example rows: "Weekdays 8:00–18:00", "Weekends 10:00–16:00".
        self.rows = rows ?? [
            WindowRow(days: .weekdays, startMinutes: 8 * 60, endMinutes: 18 * 60),
            WindowRow(days: .weekends, startMinutes: 10 * 60, endMinutes: 16 * 60),
        ]
        self.diversity = diversity
    }

    func addRow() {
        rows.append(WindowRow(days: .everyDay, startMinutes: 19 * 60, endMinutes: 21 * 60))
    }

    func removeRow(_ id: UUID) {
        guard rows.count > 1 else { return }
        rows.removeAll { $0.id == id }
    }

    /// The first problem blocking submit, if any.
    var validationIssue: ValidationIssue? {
        switch buildWindows() {
        case .failure(let issue): return issue
        case .success: return diversity == nil ? .noDiversity : nil
        }
    }

    var canSubmit: Bool { phase == .editing && validationIssue == nil }

    func buildWindows() -> Result<WeekWindows, ValidationIssue> {
        guard !rows.isEmpty else { return .failure(.noWindows) }
        var perDay: [Weekday: [(Int, Int)]] = [:]
        for row in rows {
            guard row.startMinutes < row.effectiveEnd else { return .failure(.endBeforeStart) }
            for day in row.days.days {
                perDay[day, default: []].append((row.startMinutes, row.effectiveEnd))
            }
        }
        var windows = WeekWindows()
        for day in Weekday.allCases {
            let sorted = (perDay[day] ?? []).sorted { $0.0 < $1.0 }
            var built: [TimeWindow] = []
            for (index, window) in sorted.enumerated() {
                if index > 0, window.0 < sorted[index - 1].1 { return .failure(.overlap(day)) }
                guard let start = LocalTime(Self.hhmm(window.0)), let end = LocalTime(Self.hhmm(window.1)),
                      let timeWindow = TimeWindow(start: start, end: end)
                else { return .failure(.endBeforeStart) }
                built.append(timeWindow)
            }
            windows[day] = built
        }
        return .success(windows)
    }

    /// The full PUT body. `timeZone` is the device IANA zone; `weekStart` is ISO (D20).
    func buildPreferences(timeZoneIdentifier: String, weekStart: Int) -> Result<UserPreferences, ValidationIssue> {
        guard let diversity else { return .failure(.noDiversity) }
        return buildWindows().map { windows in
            UserPreferences(
                timezone: timeZoneIdentifier,
                schedulableWindows: windows,
                dayDiversity: diversity,
                bufferMinutes: Self.defaultBufferMinutes,
                weekStart: weekStart
            )
        }
    }

    /// Sends `PUT /preferences`; returns the staged changeset (202).
    func submit(api: any APIClient, timeZoneIdentifier: String, weekStart: Int) async -> Changeset? {
        guard phase == .editing else { return nil }
        guard case .success(let preferences) = buildPreferences(timeZoneIdentifier: timeZoneIdentifier, weekStart: weekStart) else {
            return nil
        }
        phase = .submitting
        submitError = nil
        do {
            let changeset = try await api.putPreferences(preferences)
            phase = .submitted
            return changeset
        } catch {
            phase = .editing
            if let apiError = error as? APIError, let body = apiError.errorBody, body.code == KnownErrorCode.validationFailed {
                submitError = body.validationFields.first?.message ?? "Those hours didn't work. Check them and try again."
            } else if let gcal = GcalSolveFailure(error) {
                submitError = gcal.message
            } else if !(error is CancellationError) {
                submitError = "Couldn't send that. Nothing changed; try again."
            }
            return nil
        }
    }

    /// Lets the user change their answers after the proposal was rejected or went out of date.
    func reopen() {
        if phase == .submitted { phase = .editing }
    }

    /// ISO weekday (1 = Monday … 7 = Sunday) from `Calendar.firstWeekday` (1 = Sunday).
    static func isoWeekStart(calendar: Calendar = .current) -> Int {
        ((calendar.firstWeekday + 5) % 7) + 1
    }

    static func hhmm(_ minutes: Int) -> String {
        String(format: "%02d:%02d", minutes / 60, minutes % 60)
    }
}
