import Foundation

/// What a project summary card shows (design/onboarding.md §4). Fields are optional because a
/// `project_updated` for a project this client has never seen carries only the changed fields.
struct ProjectCard: Identifiable, Hashable, Sendable {
    let id: UUID
    var name: String?
    var kind: ProjectKind?
    var targetDate: LocalDate?
    var estimatedTotalHours: Double?
    var weeklyTargetHours: Double?
    var maxBlockMinutes: Int?
    var minBlockMinutes: Int?
    var requirements: [String]
    /// True for `project_created`, false for an update to an existing project.
    var isNew: Bool

    init(id: UUID, draft: ProjectDraft, isNew: Bool = true) {
        self.id = id
        name = draft.name
        kind = draft.goal.kind
        switch draft.goal {
        case let .deadline(date, hours):
            targetDate = date
            estimatedTotalHours = hours
        case let .perennial(hours):
            weeklyTargetHours = hours
        }
        maxBlockMinutes = draft.maxBlockMinutes
        minBlockMinutes = draft.minBlockMinutes
        requirements = draft.requirements ?? []
        self.isNew = isNew
    }

    init(project: Project) {
        id = project.id
        name = project.name
        kind = project.kind
        switch project.goal {
        case let .deadline(date, hours):
            targetDate = date
            estimatedTotalHours = hours
        case let .perennial(hours):
            weeklyTargetHours = hours
        }
        maxBlockMinutes = project.maxBlockMinutes
        minBlockMinutes = project.minBlockMinutes
        requirements = project.requirements
        isNew = false
    }

    /// A card for an update whose base project is unknown.
    init(unknownId id: UUID) {
        self.id = id
        requirements = []
        isNew = false
    }

    /// Overlays a staged patch for display. Not scheduling logic: it only shows the values the
    /// server staged.
    func applying(_ patch: ProjectPatch) -> ProjectCard {
        var card = self
        if let name = patch.name { card.name = name }
        if let date = patch.targetDate { card.targetDate = date; card.kind = card.kind ?? .deadline }
        if let hours = patch.estimatedTotalHours { card.estimatedTotalHours = hours; card.kind = card.kind ?? .deadline }
        if let hours = patch.weeklyTargetHours { card.weeklyTargetHours = hours; card.kind = card.kind ?? .perennial }
        if let minutes = patch.maxBlockMinutes { card.maxBlockMinutes = minutes }
        if let minutes = patch.minBlockMinutes { card.minBlockMinutes = minutes }
        if let tags = patch.requirements { card.requirements = tags }
        return card
    }
}

enum ProjectCardDerivation {
    /// One card per project touched by `project_created`/`project_updated` in `staged`, in first-
    /// appearance order. Several mutations on one project fold into one card (e.g. a creation
    /// carried forward plus its edit, O9). `base` supplies known projects for bare updates.
    static func cards(from changeset: Changeset, base: (UUID) -> ProjectCard?) -> [ProjectCard] {
        var order: [UUID] = []
        var cards: [UUID: ProjectCard] = [:]
        for mutation in changeset.staged {
            switch mutation.change {
            case let .projectCreated(projectId, draft):
                if cards[projectId] == nil { order.append(projectId) }
                cards[projectId] = ProjectCard(id: projectId, draft: draft, isNew: true)
            case let .projectUpdated(projectId, patch):
                if cards[projectId] == nil { order.append(projectId) }
                let current = cards[projectId] ?? base(projectId) ?? ProjectCard(unknownId: projectId)
                cards[projectId] = current.applying(patch)
            default:
                continue
            }
        }
        return order.compactMap { cards[$0] }
    }

    static func hasProjectCards(_ changeset: Changeset) -> Bool {
        changeset.staged.contains { $0.kind == .projectCreated || $0.kind == .projectUpdated }
    }
}

/// Card copy (design/onboarding.md §4 "Project summary card"). Display formatting only.
struct ProjectCardText {
    var locale: Locale = .current
    var calendar: Calendar = .current

    func title(_ card: ProjectCard) -> String {
        card.name ?? "Project update"
    }

    func badge(_ card: ProjectCard) -> String? {
        switch card.kind {
        case .deadline: "Deadline"
        case .perennial: "Every week"
        case nil: nil
        }
    }

    /// Line 2: target. Nil when the staged data doesn't include it.
    func goalLine(_ card: ProjectCard) -> String? {
        switch card.kind {
        case .deadline:
            var parts: [String] = []
            if let date = card.targetDate { parts.append("Due \(formatDate(date))") }
            if let hours = card.estimatedTotalHours { parts.append("about \(formatHours(hours)) h in total") }
            return parts.isEmpty ? nil : parts.joined(separator: " · ")
        case .perennial:
            return card.weeklyTargetHours.map { "\(formatHours($0)) h a week" }
        case nil:
            return nil
        }
    }

    /// Line 3: sitting length and requirement tags.
    func sessionLine(_ card: ProjectCard) -> String? {
        var parts: [String] = []
        if let max = card.maxBlockMinutes {
            var sitting = "Up to \(formatDuration(minutes: max)) per sitting"
            if let min = card.minBlockMinutes { sitting += ", at least \(formatDuration(minutes: min))" }
            parts.append(sitting)
        } else if let min = card.minBlockMinutes {
            parts.append("At least \(formatDuration(minutes: min)) per sitting")
        }
        if !card.requirements.isEmpty {
            parts.append("needs " + card.requirements.joined(separator: ", "))
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// VoiceOver label for the card's text: spelled-out units, no symbols.
    func accessibilityLabel(_ card: ProjectCard) -> String {
        var parts = [title(card)]
        switch card.kind {
        case .deadline: parts.append("deadline project")
        case .perennial: parts.append("every-week project")
        case nil: break
        }
        switch card.kind {
        case .deadline:
            if let date = card.targetDate { parts.append("due \(formatDate(date))") }
            if let hours = card.estimatedTotalHours { parts.append("about \(formatHours(hours)) hours in total") }
        case .perennial:
            if let hours = card.weeklyTargetHours { parts.append("\(formatHours(hours)) hours a week") }
        case nil:
            break
        }
        if let max = card.maxBlockMinutes { parts.append("up to \(spokenDuration(minutes: max)) per sitting") }
        if let min = card.minBlockMinutes { parts.append("at least \(spokenDuration(minutes: min))") }
        if !card.requirements.isEmpty { parts.append("needs " + card.requirements.joined(separator: ", ")) }
        return parts.joined(separator: ", ")
    }

    func formatHours(_ hours: Double) -> String {
        hours.formatted(.number.precision(.fractionLength(0...1)).locale(locale))
    }

    /// "2 h", "1 h 30 m", "45 m" (tokens.md duration style).
    func formatDuration(minutes: Int) -> String {
        let h = minutes / 60, m = minutes % 60
        switch (h, m) {
        case (0, _): return "\(m) m"
        case (_, 0): return "\(h) h"
        default: return "\(h) h \(m) m"
        }
    }

    func spokenDuration(minutes: Int) -> String {
        let h = minutes / 60, m = minutes % 60
        let hours = h == 1 ? "1 hour" : "\(h) hours"
        let mins = m == 1 ? "1 minute" : "\(m) minutes"
        switch (h, m) {
        case (0, _): return mins
        case (_, 0): return hours
        default: return "\(hours) \(mins)"
        }
    }

    /// A local date label (e.g. "Fri, Dec 12"). The date is a calendar label, not an instant, so it
    /// is formatted from its components at midday to stay clear of DST edges.
    func formatDate(_ date: LocalDate) -> String {
        let parts = date.rawValue.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return date.rawValue }
        var components = DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12)
        components.calendar = calendar
        guard let instant = calendar.date(from: components) else { return date.rawValue }
        var style = Date.FormatStyle(date: .omitted, time: .omitted, locale: locale, calendar: calendar, timeZone: calendar.timeZone)
        style = style.weekday(.abbreviated).day().month(.abbreviated)
        return instant.formatted(style)
    }
}
