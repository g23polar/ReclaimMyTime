import Foundation

/// Display formatting for spans and durations in the device calendar/time zone. Formatting only:
/// it never computes or adjusts a schedule (CLAUDE.md rule 1).
struct TimeText {
    var calendar: Calendar
    var locale: Locale

    init(calendar: Calendar = .autoupdatingCurrent, locale: Locale = .autoupdatingCurrent) {
        self.calendar = calendar
        self.locale = locale
    }

    private var base: Date.FormatStyle {
        Date.FormatStyle(date: .omitted, time: .omitted, locale: locale, calendar: calendar, timeZone: calendar.timeZone)
    }

    /// "9:00 AM" (12 h locales) / "09:00" (24 h locales).
    func time(_ date: Date) -> String {
        date.formatted(base.hour().minute())
    }

    /// The clock time without the day period ("9:00"): the locale's own format with its AM/PM
    /// symbol removed, so digit style stays identical to `time(_:)`.
    private func bareTime(_ date: Date) -> String {
        var symbols = Calendar(identifier: .gregorian)
        symbols.locale = locale
        var text = time(date)
        for symbol in [symbols.amSymbol, symbols.pmSymbol] where !symbol.isEmpty {
            text = text.replacingOccurrences(of: symbol, with: "")
        }
        return text.trimmingCharacters(in: .whitespaces.union(CharacterSet(charactersIn: "\u{202F}\u{00A0}")))
    }

    /// "9:00–11:00 AM", "11:00 AM–1:00 PM". The start drops its day period when both ends share it.
    func timeRange(_ span: Span) -> String {
        let startHour = calendar.component(.hour, from: span.start)
        let endHour = calendar.component(.hour, from: span.end)
        let sameHalf = calendar.isDate(span.start, inSameDayAs: span.end) && (startHour < 12) == (endHour < 12)
        let start = sameHalf ? bareTime(span.start) : time(span.start)
        return "\(start)–\(time(span.end))"
    }

    /// "Thu"
    func weekdayShort(_ date: Date) -> String { date.formatted(base.weekday(.abbreviated)) }
    /// "Thursday"
    func weekdayLong(_ date: Date) -> String { date.formatted(base.weekday(.wide)) }
    /// "19"
    func dayNumber(_ date: Date) -> String { date.formatted(base.day()) }
    /// "Thursday, Nov 19"
    func dayTitle(_ date: Date) -> String { date.formatted(base.weekday(.wide).month(.abbreviated).day()) }
    /// "Nov 19"
    func monthDay(_ date: Date) -> String { date.formatted(base.month(.abbreviated).day()) }

    /// "Nov 16 – 22": first and last day shown (both inclusive).
    func weekRange(first: Date, last: Date) -> String {
        guard first < last else { return monthDay(first) }
        let style = Date.IntervalFormatStyle(locale: locale, calendar: calendar, timeZone: calendar.timeZone).month(.abbreviated).day()
        return (first..<last).formatted(style)
    }

    /// "Thu 6:00 PM": a slot named by its start.
    func slot(_ span: Span) -> String {
        "\(weekdayShort(span.start)) \(time(span.start))"
    }

    /// "Thursday 6:00 PM to 8:00 PM" (VoiceOver).
    func spokenSpan(_ span: Span) -> String {
        "\(weekdayLong(span.start)) \(time(span.start)) to \(time(span.end))"
    }

    /// "Thursday 6:00–8:00 PM" (diff labels).
    func dayAndRange(_ span: Span) -> String {
        "\(weekdayLong(span.start)) \(timeRange(span))"
    }

    /// A Gregorian date label from the contract ("2026-11-27" → "Nov 27").
    func localDate(_ date: LocalDate) -> String {
        guard let instant = date.middayInstant(timeZone: calendar.timeZone) else { return date.rawValue }
        return monthDay(instant)
    }

    /// Whole minutes in a span (display only).
    static func minutes(_ span: Span) -> Int {
        Int((span.end.timeIntervalSince(span.start) / 60).rounded())
    }

    /// "2 h 30 m", "2 h", "45 m" (tokens.md duration style).
    static func duration(minutes: Int) -> String {
        let h = minutes / 60, m = minutes % 60
        switch (h, m) {
        case (0, _): return "\(m) m"
        case (_, 0): return "\(h) h"
        default: return "\(h) h \(m) m"
        }
    }

    /// "2 hours 30 minutes" (VoiceOver).
    static func spokenDuration(minutes: Int) -> String {
        let h = minutes / 60, m = minutes % 60
        let hours = h == 1 ? "1 hour" : "\(h) hours"
        let mins = m == 1 ? "1 minute" : "\(m) minutes"
        switch (h, m) {
        case (0, _): return mins
        case (_, 0): return hours
        default: return "\(hours) \(mins)"
        }
    }

    /// Hours with at most one decimal ("6.7", "3").
    func hours(_ value: Double) -> String {
        value.formatted(.number.precision(.fractionLength(0...1)).locale(locale))
    }
}
