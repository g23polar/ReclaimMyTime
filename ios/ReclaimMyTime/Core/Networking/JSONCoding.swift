import Foundation

/// The one place JSON coders are configured. Keys are mapped explicitly via `CodingKeys` (no key
/// strategy), so open `details` dictionaries keep their exact keys.
enum JSONCoding {
    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            guard let date = RFC3339.parse(raw) else {
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid RFC 3339 date-time: \(raw)")
            }
            return date
        }
        return decoder
    }

    static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(RFC3339.format(date))
        }
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return encoder
    }
}

/// RFC 3339 date-time parsing/formatting. Accepts any offset and optional fractional seconds; emits
/// UTC (`Z`), with milliseconds only when the instant has a sub-second part (so server values
/// round-trip byte-identically). Formatting/parsing only, never arithmetic.
enum RFC3339 {
    private static let withFraction = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    private static let withoutFraction = Date.ISO8601FormatStyle()

    static func parse(_ raw: String) -> Date? {
        // Fractional first: the plain style accepts ".250" but silently drops it.
        if let date = try? withFraction.parse(raw) { return date }
        if let date = try? withoutFraction.parse(raw) { return date }
        return nil
    }

    static func format(_ date: Date) -> String {
        let seconds = date.timeIntervalSince1970
        return seconds.rounded(.towardZero) == seconds ? withoutFraction.format(date) : withFraction.format(date)
    }
}
