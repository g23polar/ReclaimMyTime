import SwiftUI

/// Minimal Settings: backend mode and sign-out. The rest (calendar connection, account deletion)
/// is built in later tasks.
struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(AuthState.self) private var auth
    @State private var isSigningOut = false

    var body: some View {
        NavigationStack {
            List {
                Section("Backend") {
                    LabeledContent("Mode", value: model.config.usesMock ? "Mock fixtures" : model.config.baseURL.absoluteString)
                }
                Section {
                    Button("Sign out", role: .destructive) {
                        isSigningOut = true
                        Task {
                            await auth.signOut()
                            isSigningOut = false
                        }
                    }
                    .disabled(isSigningOut)
                }
            }
            .navigationTitle("Settings")
        }
    }
}
