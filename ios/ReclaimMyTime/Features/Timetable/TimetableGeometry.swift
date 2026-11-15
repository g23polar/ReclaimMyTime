import CoreGraphics
import Foundation

/// One displayed day: `[start, end)` of a device-calendar day.
struct DayColumn: Hashable, Identifiable, Sendable {
    let start: Date
    let end: Date
    var id: Date { start }

    func contains(_ date: Date) -> Bool { date >= start && date < end }
}

/// Vertical scale of the grid (timetable.md §1–§2). The grid geometry is fixed: it does not scale
/// with Dynamic Type (tokens.md).
struct GridMetrics: Hashable, Sendable {
    static let weekPointsPerHour: CGFloat = 44
    static let dayPointsPerHour: CGFloat = 64

    let startHour: Int
    let endHour: Int
    let pointsPerHour: CGFloat

    var height: CGFloat { CGFloat(endHour - startHour) * pointsPerHour }
    var pointsPerMinute: CGFloat { pointsPerHour / 60 }
    var hours: [Int] { Array(startHour...endHour) }

    func y(minuteOfDay minute: Double) -> CGFloat {
        CGFloat(minute - Double(startHour * 60)) * pointsPerMinute
    }
}

/// A span clipped to one column, in wall-clock minutes of that day.
struct ColumnSegment: Hashable, Sendable {
    let column: Int
    let startMinute: Double
    let endMinute: Double
}

/// Pure layout math for drawing server-provided spans on a grid. Display conversion only: nothing
/// here decides where anything is scheduled.
enum TimetableGeometry {
    static let fallbackStartHour = 7
    static let fallbackEndHour = 22

    /// `count` consecutive device-calendar days from the day containing `start`.
    static func columns(from start: Date, calendar: Calendar, count: Int = 7) -> [DayColumn] {
        let first = calendar.startOfDay(for: start)
        return (0..<count).compactMap { offset in
            guard let dayStart = calendar.date(byAdding: .day, value: offset, to: first),
                  let dayEnd = calendar.date(byAdding: .day, value: 1, to: dayStart)
            else { return nil }
            return DayColumn(start: dayStart, end: dayEnd)
        }
    }

    /// Start of the week containing `date`, with weeks starting on ISO weekday `isoWeekStart`
    /// (1 = Monday … 7 = Sunday, D20).
    static func weekStart(containing date: Date, isoWeekStart: Int, calendar: Calendar) -> Date {
        var cal = calendar
        cal.firstWeekday = (isoWeekStart % 7) + 1
        return cal.dateInterval(of: .weekOfYear, for: date)?.start ?? cal.startOfDay(for: date)
    }

    /// Wall-clock minute of `date` within `column` (0 at or before its start, 1440 at or after its end).
    static func minuteOfDay(_ date: Date, in column: DayColumn, calendar: Calendar) -> Double {
        if date <= column.start { return 0 }
        if date >= column.end { return 24 * 60 }
        let parts = calendar.dateComponents([.hour, .minute, .second], from: date)
        return Double((parts.hour ?? 0) * 60 + (parts.minute ?? 0)) + Double(parts.second ?? 0) / 60
    }

    /// The parts of `span` that fall in each column (a span crossing midnight yields two).
    static func segments(_ span: Span, columns: [DayColumn], calendar: Calendar) -> [ColumnSegment] {
        columns.enumerated().compactMap { index, column in
            guard span.start < column.end, span.end > column.start else { return nil }
            let start = minuteOfDay(max(span.start, column.start), in: column, calendar: calendar)
            let end = minuteOfDay(min(span.end, column.end), in: column, calendar: calendar)
            guard end > start else { return nil }
            return ColumnSegment(column: index, startMinute: start, endMinute: end)
        }
    }

    /// Schedulable windows (local times in `windowTimeZone`, the preferences zone) projected onto
    /// a display column, as wall-clock minute ranges. Neighbouring dates are included so a zone
    /// difference that shifts a window across midnight still lands in the right column.
    static func windowMinutes(for column: DayColumn, windows: WeekWindows, windowTimeZone: TimeZone, calendar: Calendar) -> [(Double, Double)] {
        let display = LocalDate.gregorian(timeZone: calendar.timeZone)
        let source = LocalDate.gregorian(timeZone: windowTimeZone)
        let keys: [Weekday] = [.sun, .mon, .tue, .wed, .thu, .fri, .sat]
        var result: [(Double, Double)] = []
        for offset in -1...1 {
            guard let day = display.date(byAdding: .day, value: offset, to: column.start) else { continue }
            let parts = display.dateComponents([.year, .month, .day, .weekday], from: day)
            guard let weekday = parts.weekday else { continue }
            for window in windows[keys[weekday - 1]] {
                guard let start = instant(parts, window.start, in: source),
                      let end = instant(parts, window.end, in: source),
                      let span = Span(start: start, end: end)
                else { continue }
                for segment in segments(span, columns: [column], calendar: calendar) {
                    result.append((segment.startMinute, segment.endMinute))
                }
            }
        }
        return result.sorted { $0.0 < $1.0 }
    }

    private static func instant(_ day: DateComponents, _ time: LocalTime, in calendar: Calendar) -> Date? {
        let hm = time.rawValue.split(separator: ":").compactMap { Int($0) }
        guard hm.count == 2 else { return nil }
        var parts = DateComponents(year: day.year, month: day.month, day: day.day, hour: hm[0] % 24, minute: hm[1])
        if time.isEndOfDay { parts.hour = 0 }
        guard let date = calendar.date(from: parts) else { return nil }
        return time.isEndOfDay ? calendar.date(byAdding: .day, value: 1, to: date) : date
    }

    /// Rows run from the earliest window start to the latest window end, rounded out to the hour
    /// (fallback 07:00–22:00), widened so every drawn span stays visible.
    static func metrics(
        columns: [DayColumn],
        windows: WeekWindows?,
        windowTimeZone: TimeZone?,
        spans: [Span],
        calendar: Calendar,
        pointsPerHour: CGFloat
    ) -> GridMetrics {
        var low = Double.infinity, high = -Double.infinity
        if let windows, let windowTimeZone {
            for column in columns {
                for (start, end) in windowMinutes(for: column, windows: windows, windowTimeZone: windowTimeZone, calendar: calendar) {
                    low = min(low, start)
                    high = max(high, end)
                }
            }
        }
        if low > high {
            low = Double(fallbackStartHour * 60)
            high = Double(fallbackEndHour * 60)
        }
        for span in spans {
            for segment in segments(span, columns: columns, calendar: calendar) {
                low = min(low, segment.startMinute)
                high = max(high, segment.endMinute)
            }
        }
        let startHour = max(0, Int((low / 60).rounded(.down)))
        let endHour = min(24, max(startHour + 1, Int((high / 60).rounded(.up))))
        return GridMetrics(startHour: startHour, endHour: endHour, pointsPerHour: pointsPerHour)
    }

    /// The complement of `windows` within the visible rows: what gets the outside-window shade.
    static func outsideBands(windows: [(Double, Double)], metrics: GridMetrics) -> [(Double, Double)] {
        var bands: [(Double, Double)] = []
        var cursor = Double(metrics.startHour * 60)
        let limit = Double(metrics.endHour * 60)
        for (start, end) in windows.sorted(by: { $0.0 < $1.0 }) {
            if start > cursor { bands.append((cursor, min(start, limit))) }
            cursor = max(cursor, end)
        }
        if cursor < limit { bands.append((cursor, limit)) }
        return bands.filter { $0.1 > $0.0 }
    }

    /// Frame of a segment inside the column area (x from the first column's leading edge).
    static func rect(_ segment: ColumnSegment, metrics: GridMetrics, columnWidth: CGFloat, inset: CGFloat = 1) -> CGRect {
        let top = metrics.y(minuteOfDay: segment.startMinute)
        let bottom = metrics.y(minuteOfDay: segment.endMinute)
        return CGRect(
            x: CGFloat(segment.column) * columnWidth + inset,
            y: top,
            width: max(0, columnWidth - inset * 2),
            height: max(1, bottom - top)
        )
    }
}
