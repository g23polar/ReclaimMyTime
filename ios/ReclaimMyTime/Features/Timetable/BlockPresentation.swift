import SwiftUI

/// How a block is drawn (timetable.md §3). Derived from the server's `state` plus the device clock;
/// the client never changes a block's state itself.
enum BlockTreatment: Hashable, Sendable {
    /// `planned`, start > now.
    case planned
    /// `planned`, start ≤ now < end.
    case inProgress
    /// `planned`, end ≤ now: the server's lazy sweep hasn't marked it yet. Frozen, drawn as planned.
    case pastPlanned
    case completed
    case skipped
}

struct BlockClassification: Hashable, Sendable {
    let treatment: BlockTreatment
    let isPinned: Bool

    /// Past and in-progress blocks are frozen (D7, `block_immutable`).
    var isFrozen: Bool { treatment != .planned }
    /// Started blocks offer Mark done / I skipped this (D5); the current state is not re-offered.
    var offersMarkDone: Bool { isFrozen && treatment != .completed }
    var offersSkip: Bool { isFrozen && treatment != .skipped }
    var stateWord: String {
        switch treatment {
        case .completed: "Completed"
        case .skipped: "Skipped"
        case .planned, .inProgress, .pastPlanned: "Planned"
        }
    }
}

enum BlockClassifier {
    static func classify(_ block: Block, now: Date, pinnedIds: Set<UUID>) -> BlockClassification {
        let treatment: BlockTreatment
        switch block.state {
        case .completed: treatment = .completed
        case .skipped: treatment = .skipped
        case .planned:
            if block.span.start > now {
                treatment = .planned
            } else if now < block.span.end {
                treatment = .inProgress
            } else {
                treatment = .pastPlanned
            }
        }
        return BlockClassification(treatment: treatment, isPinned: pinnedIds.contains(block.id))
    }
}

/// Design gap G2: `Block` has no pinned flag, so a block is pinned when a `kind: pin` row has the
/// same project and exactly the same span.
enum PinJoin {
    static func pinnedBlockIds(blocks: [Block], pins: [BlockPin]) -> Set<UUID> {
        var keys = Set<PinKey>()
        for pin in pins where pin.kind == .pin {
            if let projectId = pin.projectId { keys.insert(PinKey(projectId: projectId, span: pin.span)) }
        }
        return Set(blocks.filter { keys.contains(PinKey(projectId: $0.projectId, span: $0.span)) }.map(\.id))
    }

    static func forbids(_ pins: [BlockPin]) -> [BlockPin] {
        pins.filter { $0.kind == .forbid }
    }

    private struct PinKey: Hashable {
        let projectId: UUID
        let span: Span
    }
}

/// An edit to a live block that is staged but not confirmed: drawn as a proposal in place
/// (timetable.md §5) until the changeset resolves.
enum StagedBlockEdit: Hashable, Sendable {
    case moved(to: Span)
    case deleted(Span)
}

enum StagedEdits {
    /// Reads `pin_added` mutations that came from a block (`created_from_block_id`) in the pending
    /// changeset: a pin is an edit, a forbid is a delete (D6).
    static func from(_ changeset: Changeset?) -> [UUID: StagedBlockEdit] {
        guard let changeset else { return [:] }
        var edits: [UUID: StagedBlockEdit] = [:]
        for mutation in changeset.staged {
            guard case .pinAdded(let payload) = mutation.change, let blockId = payload.createdFromBlockId else { continue }
            edits[blockId] = payload.kind == .pin ? .moved(to: payload.span) : .deleted(payload.span)
        }
        return edits
    }
}

/// Dynamic Type behaviour of the timetable (tokens.md, timetable.md §1).
enum TimetableLayoutMode: Hashable, Sendable {
    case grid
    case list

    static func mode(for size: DynamicTypeSize) -> TimetableLayoutMode {
        size.isAccessibilitySize ? .list : .grid
    }
}

/// What a block's body shows (timetable.md §4): line 2 only for blocks ≥ 45 min and outside AX
/// sizes, where blocks show the project name only.
enum BlockContent: Hashable, Sendable {
    case nameOnly
    case nameAndMeta

    static func content(minutes: Int, dynamicTypeSize: DynamicTypeSize, compact: Bool) -> BlockContent {
        if compact || dynamicTypeSize.isAccessibilitySize || minutes < 45 { return .nameOnly }
        return .nameAndMeta
    }
}

/// Block copy: VoiceOver label and the AX-size list row.
struct BlockText {
    var time: TimeText

    /// "{project}, {weekday} {start} to {end}, {duration}. {Pinned.} {Completed|Skipped|Planned}.
    /// {Not yet in Google Calendar.}"
    func accessibilityLabel(_ block: Block, project: String, classification: BlockClassification) -> String {
        var parts = ["\(project), \(time.spokenSpan(block.span)), \(TimeText.spokenDuration(minutes: TimeText.minutes(block.span)))."]
        if classification.isPinned { parts.append("Pinned.") }
        parts.append("\(classification.stateWord).")
        if block.gcalStatus != .synced { parts.append("Not yet in Google Calendar.") }
        return parts.joined(separator: " ")
    }

    /// "Mon 9:00–11:00 AM · Portfolio site · 2 h"
    func listRow(_ block: Block, project: String) -> String {
        "\(time.weekdayShort(block.span.start)) \(time.timeRange(block.span)) · \(project) · \(TimeText.duration(minutes: TimeText.minutes(block.span)))"
    }

    /// "9:00–11:00 AM · 2 h"
    func meta(_ span: Span) -> String {
        "\(time.timeRange(span)) · \(TimeText.duration(minutes: TimeText.minutes(span)))"
    }
}

/// The AX-size list layout: blocks grouped by day, in plan order.
struct DayListSection: Identifiable, Hashable {
    let day: DayColumn
    let title: String
    let blocks: [Block]
    var id: Date { day.start }
}

enum DayListLayout {
    static func sections(blocks: [Block], columns: [DayColumn], time: TimeText) -> [DayListSection] {
        columns.compactMap { column in
            let dayBlocks = blocks.filter { column.contains($0.span.start) }.sorted { $0.span.start < $1.span.start }
            guard !dayBlocks.isEmpty else { return nil }
            return DayListSection(day: column, title: time.dayTitle(column.start), blocks: dayBlocks)
        }
    }
}
