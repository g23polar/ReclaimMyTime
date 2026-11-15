import SwiftUI

/// Settings: Google Calendar connection, backend mode and sign-out. Account deletion and the rest
/// come in later tasks.
struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(AuthState.self) private var auth
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession

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

    private func connectButton(title: String) -> some View {
        Button(title) {
            let session = webAuthenticationSession
            let usesMock = model.config.usesMock
            Task {
                await model.calendar.connect { url, scheme in
                    if usesMock { return MockWebAuthenticator.callbackURL() }
                    return try await session.authenticate(using: url, callbackURLScheme: scheme)
                }
            }
        }
        .disabled(model.calendar.isConnecting)
    }
}
