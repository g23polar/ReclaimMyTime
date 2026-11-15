import Foundation

/// One lineage in a proposal's diff (diff-and-infeasibility.md §2), keyed by `lineage_id` (D18).
struct DiffItem: Identifiable, Hashable, Sendable {
    enum Kind: Hashable, Sendable {
        case added, removed, moved, unchanged
    }

    let lineageId: UUID
    let projectId: UUID
    let kind: Kind
    let before: Span?
    let after: Span?
    /// The proposed plan's block for this lineage (nil for `removed`).
    let block: Block?

    var id: UUID { lineageId }

    /// A `moved` entry whose start or end stayed put: only the length changed (D18 counts resizes
    /// as moves). Labelled "Now {duration}" instead of drawing a connector.
    var isResizeOnly: Bool {
        guard kind == .moved, let before, let after else { return false }
        return before.start == after.start || before.end == after.end
    }

    /// Where the item sits on the grid: `after`, or `before` for removed.
    var displaySpan: Span? { after ?? before }
}

/// The review screen's model of a pending changeset's diff. Pure: it only reads what the server
/// sent.
struct DiffModel: Hashable, Sendable {
    let items: [DiffItem]
    /// `against_plan_id` null: the whole week is new.
    let isFirstWeek: Bool
    let plan: WeekPlan

    init?(_ changeset: Changeset) {
        guard let plan = changeset.plan, let diff = changeset.diff else { return nil }
        let blocksByLineage = Dictionary(plan.blocks.map { ($0.lineageId, $0) }, uniquingKeysWith: { first, _ in first })
        self.plan = plan
        isFirstWeek = diff.againstPlanId == nil
        items = diff.entries.map { entry in
            let kind: DiffItem.Kind = switch entry.change {
            case .added: .added
            case .removed: .removed
            case .moved: .moved
            case .unchanged: .unchanged
            }
            return DiffItem(
                lineageId: entry.lineageId,
                projectId: entry.projectId,
                kind: kind,
                before: entry.change.before,
                after: entry.change.after,
                block: blocksByLineage[entry.lineageId]
            )
        }
    }

    var title: String { isFirstWeek ? "Your first week" : "Review changes" }

    /// Per-block "New" label and badge are suppressed in first-week mode: everything is new.
    var showsNewBadges: Bool { !isFirstWeek }

    var changes: [DiffItem] { items.filter { $0.kind != .unchanged } }

    func count(_ kind: DiffItem.Kind) -> Int { items.filter { $0.kind == kind }.count }

    /// Every span the grid has to show (both sides), for sizing the rows.
    var allSpans: [Span] { items.flatMap { [$0.before, $0.after].compactMap { $0 } } }

    /// The list alternative: changes only, grouped by the day of their display span.
    func changesByDay(columns: [DayColumn]) -> [(day: DayColumn, items: [DiffItem])] {
        columns.compactMap { column in
            let dayItems = changes.filter { item in item.displaySpan.map { column.contains($0.start) } ?? false }
            return dayItems.isEmpty ? nil : (column, dayItems)
        }
    }
}

/// Diff copy: VoiceOver labels and list rows (diff-and-infeasibility.md §2).
struct DiffText {
    var time: TimeText

    func accessibilityLabel(_ item: DiffItem, project: String, firstWeek: Bool) -> String {
        switch item.kind {
        case .unchanged:
            return "\(project), \(span(item.after)), unchanged"
        case .added:
            return firstWeek ? "\(project), \(span(item.after))" : "New: \(project), \(span(item.after))"
        case .removed:
            return "Removed: \(project), \(span(item.before))"
        case .moved:
            if item.isResizeOnly, let after = item.after {
                return "\(project), \(span(after)), now \(TimeText.spokenDuration(minutes: TimeText.minutes(after)))"
            }
            return "Moved: \(project), from \(span(item.before)) to \(span(item.after))"
        }
    }

    /// The short label drawn on a resized block.
    func resizeLabel(_ item: DiffItem) -> String? {
        guard item.isResizeOnly, let after = item.after else { return nil }
        return "Now \(TimeText.duration(minutes: TimeText.minutes(after)))"
    }

    private func span(_ span: Span?) -> String {
        span.map(time.dayAndRange) ?? ""
    }
}
