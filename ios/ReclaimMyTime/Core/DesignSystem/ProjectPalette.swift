import Foundation
import SwiftUI

/// The fixed 10-slot project palette (design/tokens.md "Project palette"). Each slot has a `fill`
/// (block background) and an `ink` (text on that fill), tuned per appearance in the asset catalog.
/// Color helps scanning; the project name is always what identifies a block.
struct PaletteSlot: Hashable, Sendable {
    static let count = 10
    static let names = ["Blue", "Green", "Orange", "Purple", "Teal", "Pink", "Yellow", "Slate", "Brown", "Lime"]

    let index: Int

    init(_ index: Int) {
        self.index = ((index % Self.count) + Self.count) % Self.count
    }

    var name: String { Self.names[index] }
    var fill: Color { Color(Self.fills[index]) }
    var ink: Color { Color(Self.inks[index]) }
    /// 3 pt leading edge bar: the slot's ink at 70%.
    var edge: Color { ink.opacity(0.7) }

    private static let fills: [ColorResource] = [
        .projectFill0, .projectFill1, .projectFill2, .projectFill3, .projectFill4,
        .projectFill5, .projectFill6, .projectFill7, .projectFill8, .projectFill9,
    ]
    private static let inks: [ColorResource] = [
        .projectInk0, .projectInk1, .projectInk2, .projectInk3, .projectInk4,
        .projectInk5, .projectInk6, .projectInk7, .projectInk8, .projectInk9,
    ]
}

/// The assignment rule, as a pure function so it is unit-testable.
enum PaletteAssignment {
    /// Lowest slot not held by an active project. When all 10 are held, slots reuse.
    /// The spec leaves the reuse order open; this picks the least-used slot (ties → lowest index)
    /// so reuse spreads evenly. Deterministic.
    static func nextSlot(activeAssignments: [UUID: Int]) -> Int {
        var usage = [Int](repeating: 0, count: PaletteSlot.count)
        for slot in activeAssignments.values where (0..<PaletteSlot.count).contains(slot) {
            usage[slot] += 1
        }
        let minUsage = usage.min() ?? 0
        return usage.firstIndex(of: minUsage) ?? 0
    }
}

/// Client-side slot storage keyed by `project_id` (no contract field). A project keeps its slot
/// for life; `release` frees it when the project is no longer active (archived/done/staged-then-
/// rejected), so the next project can take the lowest free slot.
@MainActor
final class ProjectPaletteStore {
    static let defaultsKey = "projectPaletteSlots"

    private let defaults: UserDefaults
    private var assignments: [UUID: Int]

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let stored = defaults.dictionary(forKey: Self.defaultsKey) as? [String: Int] ?? [:]
        var parsed: [UUID: Int] = [:]
        for (key, slot) in stored {
            if let id = UUID(uuidString: key) { parsed[id] = slot }
        }
        assignments = parsed
    }

    /// The project's slot, assigning the next one on first sight.
    func slot(for projectId: UUID) -> PaletteSlot {
        if let existing = assignments[projectId] { return PaletteSlot(existing) }
        let slot = PaletteAssignment.nextSlot(activeAssignments: assignments)
        assignments[projectId] = slot
        persist()
        return PaletteSlot(slot)
    }

    func release(_ projectId: UUID) {
        guard assignments.removeValue(forKey: projectId) != nil else { return }
        persist()
    }

    private func persist() {
        var raw: [String: Int] = [:]
        for (id, slot) in assignments { raw[id.uuidString] = slot }
        defaults.set(raw, forKey: Self.defaultsKey)
    }
}
