import SwiftUI

/// Switches on `AuthState.phase`: signed out → intro (first run) then sign-in; signed in → tabs.
struct RootView: View {
    @Environment(AuthState.self) private var auth
    @AppStorage("hasSeenIntro") private var hasSeenIntro = false

    var body: some View {
        Group {
            switch auth.phase {
            case .loading:
                ProgressView()
            case .signedOut:
                if hasSeenIntro {
                    SignInView()
                } else {
                    OnboardingIntroView(onFinish: { hasSeenIntro = true })
                }
            case .signedIn:
                MainTabView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(ColorToken.bg)
        .task { await auth.bootstrap() }
    }
}

/// The 4-tab shell (design/README.md "App shell"): Week · Chat · Projects · Settings.
/// Day view is pushed from Week, not a tab.
struct MainTabView: View {
    var body: some View {
        TabView {
            WeekPlaceholderView()
                .tabItem { Label("Week", systemImage: "calendar") }
            ChatPlaceholderView()
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
            ProjectsPlaceholderView()
                .tabItem { Label("Projects", systemImage: "folder") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}
