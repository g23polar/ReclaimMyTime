import Foundation
import UserNotifications

/// `UNUserNotificationCenterDelegate`: turns a response into a `NotificationRoute` (plain values)
/// and hands it to the app on the main actor. Installed at launch, before the app finishes
/// launching, so a tap that cold-starts the app is delivered.
final class NotificationResponder: NSObject, UNUserNotificationCenterDelegate, Sendable {
    typealias Handler = @MainActor @Sendable (NotificationRoute) async -> Void

    private let handler: Handler

    init(handler: @escaping Handler) {
        self.handler = handler
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        var info: [String: String] = [:]
        for (key, value) in response.notification.request.content.userInfo {
            if let key = key as? String, let value = value as? String { info[key] = value }
        }
        guard let route = NotificationRoute(
            actionIdentifier: response.actionIdentifier,
            userInfo: info,
            defaultAction: UNNotificationDefaultActionIdentifier,
            dismissAction: UNNotificationDismissActionIdentifier
        ) else { return }
        await handler(route)
    }

    /// In the foreground a block start still shows as a banner: the app may be on another tab.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }
}
