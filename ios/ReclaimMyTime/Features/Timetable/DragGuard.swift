import Foundation

/// Client-side drag rules (timetable.md §5). These are contract rules (D7) and input snapping, not
/// solver logic: the dropped span is the user's intent, sent as `edit`; the server re-solves.
enum DragGuard {
    static let snapMinutes = 5
    /// Minimum span a resize can leave (one snap step).
    static let minimumMinutes = 5
    /// Light haptic on each 15-minute tick.
    static let tickMinutes = 15

    enum Handle: Hashable, Sendable {
        case body
        case top
        case bottom
    }

    enum Decision: Hashable, Sendable {
        case send(Span)
        /// Dropped (partly) before now: snap back with a rigid haptic (the server would 422).
        case rejectedPast
        /// Released where it started: nothing to send.
        case unchanged
    }

    /// Past/in-progress/completed/skipped blocks can't be picked up (D7).
    static func canPickUp(_ classification: BlockClassification) -> Bool {
        !classification.isFrozen
    }

    /// Nearest 5-minute mark on the absolute timeline (every time-zone offset is a multiple of
    /// 5 minutes, so this is also a 5-minute mark on the wall clock).
    static func snap(_ date: Date) -> Date {
        let step = Double(snapMinutes * 60)
        return Date(timeIntervalSinceReferenceDate: (date.timeIntervalSinceReferenceDate / step).rounded() * step)
    }

    /// The span a drag proposes. `bounds` (the visible day, clipped to the plan horizon) keeps the
    /// block on its day; there is no cross-week drag in v1 (D7).
    static func proposedSpan(original: Span, handle: Handle, deltaMinutes: Double, bounds: Span) -> Span {
        let delta = deltaMinutes * 60
        let minimum = Double(minimumMinutes * 60)
        switch handle {
        case .body:
            let duration = original.end.timeIntervalSince(original.start)
            var start = snap(original.start.addingTimeInterval(delta))
            if start < bounds.start { start = bounds.start }
            if start.addingTimeInterval(duration) > bounds.end { start = bounds.end.addingTimeInterval(-duration) }
            return Span(start: start, end: start.addingTimeInterval(duration)) ?? original
        case .top:
            var start = snap(original.start.addingTimeInterval(delta))
            start = max(bounds.start, min(start, original.end.addingTimeInterval(-minimum)))
            return Span(start: start, end: original.end) ?? original
        case .bottom:
            var end = snap(original.end.addingTimeInterval(delta))
            end = min(bounds.end, max(end, original.start.addingTimeInterval(minimum)))
            return Span(start: original.start, end: end) ?? original
        }
    }

    static func decide(original: Span, proposed: Span, now: Date) -> Decision {
        if proposed == original { return .unchanged }
        if proposed.start < now { return .rejectedPast }
        return .send(proposed)
    }

    /// Changes each time the moving edge crosses a 15-minute mark (drives the light haptic).
    static func tick(_ span: Span, handle: Handle) -> Int {
        let edge = handle == .bottom ? span.end : span.start
        return Int((edge.timeIntervalSinceReferenceDate / Double(tickMinutes * 60)).rounded(.down))
    }
}
