import Foundation

/// `UserPreferences`: the row and the full-replacement `PUT /preferences` body.
struct UserPreferences: Codable, Hashable, Sendable {
    /// IANA name. Compared with `TimeZone.current.identifier` for mismatch detection (T5.4).
    var timezone: String
    var schedulableWindows: WeekWindows
    var dayDiversity: DayDiversity
    var bufferMinutes: Int
    /// ISO weekday, 1 = Monday … 7 = Sunday (D20).
    var weekStart: Int

    private enum CodingKeys: String, CodingKey {
        case timezone
        case schedulableWindows = "schedulable_windows"
        case dayDiversity = "day_diversity"
        case bufferMinutes = "buffer_minutes"
        case weekStart = "week_start"
    }
}
