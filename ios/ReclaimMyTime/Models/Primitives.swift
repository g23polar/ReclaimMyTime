import Foundation

// Primitive contract types (contracts/openapi.yaml "primitives", contracts/NOTES.md "Time and the
// tstzrange mapping"). These carry values only: the client never does scheduling arithmetic on them.

/// A `tstzrange` mapped to `{start, end}` (D1). Half-open `[start, end)`; `start < end` is enforced
/// on decode so a malformed server payload fails loudly instead of being drawn.
struct Span: Codable, Hashable, Sendable {
    let start: Date
    let end: Date

    /// Returns nil when `start >= end`.
    init?(start: Date, end: Date) {
        guard start < end else { return nil }
        self.start = start
        self.end = end
    }

    private enum CodingKeys: String, CodingKey {
        case start, end
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let start = try container.decode(Date.self, forKey: .start)
        let end = try container.decode(Date.self, forKey: .end)
        guard start < end else {
            throw DecodingError.dataCorruptedError(
                forKey: .end, in: container,
                debugDescription: "span start >= end"
            )
        }
        self.start = start
        self.end = end
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(start, forKey: .start)
        try container.encode(end, forKey: .end)
    }
}

/// `format: date`: a local calendar date `YYYY-MM-DD`. Kept as its wire string; it is a label in the
/// user's preferences timezone, not an instant, so converting it to `Date` client-side would be wrong.
struct LocalDate: Codable, Hashable, Sendable, Comparable, CustomStringConvertible {
    let rawValue: String

    init?(_ rawValue: String) {
        guard LocalDate.isValid(rawValue) else { return nil }
        self.rawValue = rawValue
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = try container.decode(String.self)
        guard LocalDate.isValid(raw) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid local date: \(raw)")
        }
        rawValue = raw
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }

    var description: String { rawValue }

    /// ISO dates compare correctly as strings.
    static func < (lhs: LocalDate, rhs: LocalDate) -> Bool { lhs.rawValue < rhs.rawValue }

    private static func isValid(_ raw: String) -> Bool {
        let parts = raw.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 4, parts[1].count == 2, parts[2].count == 2,
              parts.allSatisfy({ $0.allSatisfy(\.isASCII) && $0.allSatisfy(\.isNumber) }),
              let month = Int(parts[1]), let day = Int(parts[2])
        else { return false }
        return (1...12).contains(month) && (1...31).contains(day)
    }
}

/// `LocalTime`: wall-clock `HH:MM` (24h). `"24:00"` is valid only as a window end.
struct LocalTime: Codable, Hashable, Sendable, Comparable, CustomStringConvertible {
    let rawValue: String

    init?(_ rawValue: String) {
        guard LocalTime.isValid(rawValue) else { return nil }
        self.rawValue = rawValue
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = try container.decode(String.self)
        guard LocalTime.isValid(raw) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid local time: \(raw)")
        }
        rawValue = raw
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }

    var description: String { rawValue }
    var isEndOfDay: Bool { rawValue == "24:00" }

    /// Zero-padded `HH:MM` compares correctly as a string (and "24:00" sorts last).
    static func < (lhs: LocalTime, rhs: LocalTime) -> Bool { lhs.rawValue < rhs.rawValue }

    private static func isValid(_ raw: String) -> Bool {
        if raw == "24:00" { return true }
        let parts = raw.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 2, parts[0].count == 2, parts[1].count == 2,
              parts.allSatisfy({ $0.allSatisfy(\.isASCII) && $0.allSatisfy(\.isNumber) }),
              let hour = Int(parts[0]), let minute = Int(parts[1])
        else { return false }
        return (0...23).contains(hour) && (0...59).contains(minute)
    }
}

/// `TimeWindow`: `[start, end)` local times on one day, encoded as a 2-element JSON array.
struct TimeWindow: Codable, Hashable, Sendable {
    let start: LocalTime
    let end: LocalTime

    init?(start: LocalTime, end: LocalTime) {
        guard start < end, !start.isEndOfDay else { return nil }
        self.start = start
        self.end = end
    }

    init(from decoder: any Decoder) throws {
        var container = try decoder.unkeyedContainer()
        let start = try container.decode(LocalTime.self)
        let end = try container.decode(LocalTime.self)
        guard container.isAtEnd else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "TimeWindow must have exactly 2 items")
        }
        guard start < end, !start.isEndOfDay else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "TimeWindow start must precede end")
        }
        self.start = start
        self.end = end
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.unkeyedContainer()
        try container.encode(start)
        try container.encode(end)
    }
}

/// Weekday keys of `WeekWindows` (`mon`…`sun`).
enum Weekday: String, Codable, CaseIterable, Hashable, Sendable {
    case mon, tue, wed, thu, fri, sat, sun
}

/// `WeekWindows`: jsonb weekly windows keyed `mon`…`sun`. An absent key equals `[]`.
/// Encoding omits empty days (equivalent on the wire).
struct WeekWindows: Codable, Hashable, Sendable {
    private var days: [Weekday: [TimeWindow]]

    init(_ days: [Weekday: [TimeWindow]] = [:]) {
        self.days = days.filter { !$0.value.isEmpty }
    }

    subscript(day: Weekday) -> [TimeWindow] {
        get { days[day] ?? [] }
        set { days[day] = newValue.isEmpty ? nil : newValue }
    }

    private struct DayKey: CodingKey {
        let stringValue: String
        var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: DayKey.self)
        var days: [Weekday: [TimeWindow]] = [:]
        for day in Weekday.allCases {
            if let windows = try container.decodeIfPresent([TimeWindow].self, forKey: DayKey(stringValue: day.rawValue)),
               !windows.isEmpty {
                days[day] = windows
            }
        }
        self.days = days
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: DayKey.self)
        for day in Weekday.allCases {
            if let windows = days[day], !windows.isEmpty {
                try container.encode(windows, forKey: DayKey(stringValue: day.rawValue))
            }
        }
    }
}

/// A field that can be absent, explicitly `null`, or a value. Used with `Optional` for PATCH bodies
/// where `null` means "clear" (e.g. `ProjectPatch.preferred_windows`).
enum Nullable<Wrapped>: Hashable, Sendable where Wrapped: Hashable & Sendable {
    case null
    case value(Wrapped)

    var value: Wrapped? {
        if case .value(let wrapped) = self { return wrapped }
        return nil
    }
}

/// Arbitrary JSON, used for open-ended `details` objects in the error envelope.
enum JSONValue: Codable, Hashable, Sendable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON value")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }

    var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }
}
