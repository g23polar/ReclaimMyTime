import Foundation
import UserNotifications

/// A local notification as this app describes it: plain, `Sendable` values only, so planning and
/// tests never touch `UNNotificationRequest`.
struct LocalNotificationRequest: Hashable, Sendable {
    let identifier: String
    let title: String
    let body: String
    let categoryIdentifier: String
    /// Absolute fire time; nil delivers immediately.
    let fireDate: Date?
    let userInfo: [String: String]
}

enum NotificationAuthorization: Hashable, Sendable {
    case notDetermined
    case denied
    /// Authorized, provisional or ephemeral: requests will be delivered.
    case authorized
}

/// Category and action identifiers (design/notifications.md §1–§2, §4).
enum NotificationCategoryID {
    static let blockStart = "BLOCK_START"
    static let blockSoon = "BLOCK_SOON"
    static let drift = "CALENDAR_DRIFT"
}

enum NotificationActionID {
    static let skip = "SKIP_BLOCK"
    static let open = "OPEN_BLOCK"
}

/// The only door to `UNUserNotificationCenter`. Tests substitute a fake.
protocol LocalNotificationCenter: Sendable {
    func authorization() async -> NotificationAuthorization
    /// Shows the system prompt (once per install). Returns whether delivery is now allowed.
    func requestAuthorization() async -> Bool
    func registerCategories() async
    func pendingIdentifiers() async -> [String]
    /// Adds or, for an existing identifier, replaces.
    func add(_ request: LocalNotificationRequest) async throws
    func removePending(identifiers: [String]) async
}

/// Production implementation over `UNUserNotificationCenter.current()`.
struct SystemNotificationCenter: LocalNotificationCenter {
    func authorization() async -> NotificationAuthorization {
        switch await UNUserNotificationCenter.current().notificationSettings().authorizationStatus {
        case .notDetermined: .notDetermined
        case .denied: .denied
        default: .authorized
        }
    }

    func requestAuthorization() async -> Bool {
        (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
    }

    func registerCategories() async {
        let skip = UNNotificationAction(identifier: NotificationActionID.skip, title: "Skip this", options: [])
        let open = UNNotificationAction(identifier: NotificationActionID.open, title: "Open", options: [.foreground])
        UNUserNotificationCenter.current().setNotificationCategories([
            UNNotificationCategory(identifier: NotificationCategoryID.blockStart, actions: [skip, open], intentIdentifiers: []),
            UNNotificationCategory(identifier: NotificationCategoryID.blockSoon, actions: [open], intentIdentifiers: []),
            UNNotificationCategory(identifier: NotificationCategoryID.drift, actions: [], intentIdentifiers: []),
        ])
    }

    func pendingIdentifiers() async -> [String] {
        await UNUserNotificationCenter.current().pendingNotificationRequests().map(\.identifier)
    }

    func add(_ request: LocalNotificationRequest) async throws {
        let content = UNMutableNotificationContent()
        content.title = request.title
        content.body = request.body
        content.categoryIdentifier = request.categoryIdentifier
        content.userInfo = request.userInfo
        content.sound = .default
        var trigger: UNNotificationTrigger?
        if let fireDate = request.fireDate {
            // An interval keeps the fire time absolute: a calendar trigger would follow the wall
            // clock into a new time zone, and blocks are instants.
            let interval = fireDate.timeIntervalSinceNow
            guard interval > 0 else { return }
            trigger = UNTimeIntervalNotificationTrigger(timeInterval: interval, repeats: false)
        }
        try await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: request.identifier, content: content, trigger: trigger))
    }

    func removePending(identifiers: [String]) async {
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: identifiers)
    }
}
