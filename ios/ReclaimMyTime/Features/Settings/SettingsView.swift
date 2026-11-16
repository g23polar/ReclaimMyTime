import SwiftUI
import UIKit

/// Settings: Google Calendar connection, notifications, backend mode and sign-out. Account deletion and the rest
/// come in later tasks.
struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(AuthState.self) private var auth
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    @Environment(\.openURL) private var openURL

    var body: some View {
        let calendar = model.calendar
        NavigationStack {
            List {
                Section("Google Calendar") {
                    switch calendar.status {
                    case .connected:
                        LabeledContent("Connected", value: calendar.connectedEmail ?? "Google Calendar")
                    case .revoked:
                        Label("Google access was removed. Reconnect to plan around your meetings.", systemImage: "exclamationmark.arrow.triangle.2.circlepath")
                            .foregroundStyle(ColorToken.warning)
                        connectButton(title: "Reconnect Google Calendar")
                    case .notConnected, .unknown:
                        Label("Calendar not connected", systemImage: "calendar")
                            .foregroundStyle(ColorToken.textSecondary)
                        connectButton(title: "Connect Google Calendar")
                    }
                    if let callout = calendar.callout {
                        Text(callout.message)
                            .font(.rmt.callout)
                            .foregroundStyle(ColorToken.warning)
                    }
                }
                notificationsSection
                Section("Backend") {
                    LabeledContent("Mode", value: model.config.usesMock ? "Mock fixtures" : model.config.baseURL.absoluteString)
                }
                Section {
                    Button("Sign out", role: .destructive) {
                        Task { await auth.signOut() }
                    }
                }
            }
            .navigationTitle("Settings")
            .task { await calendar.refreshStatus() }
        }
    }

    /// design/notifications.md §2: "Heads-up 10 minutes before" (off by default).
    @ViewBuilder
    private var notificationsSection: some View {
        @Bindable var notifications = model.notifications
        Section {
            Toggle("Heads-up 10 minutes before", isOn: $notifications.warningsEnabled)
            switch notifications.authorization {
            case .authorized:
                EmptyView()
            case .notDetermined:
                Button("Turn on notifications") {
                    Task { await notifications.requestFromSettings() }
                }
            case .denied:
                // SwiftUI has no settings-URL API; the UIKit constant is the only route.
                Button("Notifications are off. Open iOS Settings") {
                    if let url = URL(string: UIApplication.openNotificationSettingsURLString) { openURL(url) }
                }
            }
        } header: {
            Text("Notifications")
        } footer: {
            Text("A nudge when each block starts.")
        }
        .task { await notifications.refreshAuthorization() }
    }

    private func connectButton(title: String) -> some View {
        Button(title) {
            let session = webAuthenticationSession
            let usesMock = model.config.usesMock
            Task {
                await model.calendar.connect(using: GcalWebAuthenticator.make(session: session, usesMock: usesMock))
            }
        }
        .disabled(model.calendar.isConnecting)
    }
}
