import SwiftUI

/// Onboarding flow (design/onboarding.md): intro placeholder → Sign in with Apple → Google Calendar
/// consent → Chat. Returning users go straight to the tabs.
struct RootView: View {
    @Environment(AppModel.self) private var model
    @Environment(AuthState.self) private var auth
    @AppStorage("hasSeenIntro") private var hasSeenIntro = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            switch auth.phase {
            case .loading:
                ProgressView()
            case .sessionUnavailable:
                SessionUnavailableView { Task { await auth.bootstrap() } }
            case .signedOut:
                if hasSeenIntro {
                    SignInView()
                } else {
                    OnboardingIntroView(onFinish: { hasSeenIntro = true })
                }
            case .signedIn(let session):
                if model.calendar.needsConsentStep(userId: session.userId) {
                    GoogleCalendarConsentView(userId: session.userId)
                } else {
                    MainTabView()
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(ColorToken.bg)
        .task {
            await auth.bootstrap()
            #if DEBUG
            await model.runDemoScenarioIfRequested()
            #endif
        }
        .onChange(of: auth.phase, initial: true) { _, phase in
            model.handleAuthPhase(phase)
        }
        .onChange(of: scenePhase) { _, phase in
            model.handleScenePhase(phase)
        }
    }
}

/// The Keychain couldn't be read (e.g. before first unlock). Not a sign-out: offer a retry.
struct SessionUnavailableView: View {
    let retry: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label("Can't open your account yet", systemImage: "lock")
        } description: {
            Text("Your device's keychain isn't available right now. Unlock your device and try again.")
        } actions: {
            Button("Try again", action: retry)
                .buttonStyle(.borderedProminent)
        }
    }
}

/// The 4-tab shell (design/README.md "App shell"): Week · Chat · Projects · Settings.
/// The pending-changeset banner sits above the tab bar on every tab (timetable.md §6).
struct MainTabView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var navigation = model.navigation
        let timezone = model.timezone
        let timezonePrompt = Binding<TimezoneMonitor.Prompt?>(
            get: { navigation.selectedTab == .week && !navigation.isReviewingProposal ? timezone.prompt : nil },
            set: { value in
                if value == nil, timezone.prompt != nil, !timezone.isReplanning { timezone.dismissForNow() }
            }
        )
        TabView(selection: $navigation.selectedTab) {
            WeekView()
                .pendingChangesetBanner { NotificationPrePromptCard() }
                .sheet(item: timezonePrompt, onDismiss: {
                    if timezone.consumeReviewRequest() { navigation.presentReview() }
                }) { prompt in
                    TimezoneChangeSheet(prompt: prompt)
                }
                .tabItem { Label("Week", systemImage: "calendar") }
                .tag(AppTab.week)
            ChatView() // draws the banner itself, below its composer
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
                .tag(AppTab.chat)
            ProjectsPlaceholderView()
                .pendingChangesetBanner()
                .tabItem { Label("Projects", systemImage: "folder") }
                .tag(AppTab.projects)
            SettingsView()
                .pendingChangesetBanner()
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(AppTab.settings)
        }
        .safeAreaInset(edge: .top, spacing: 0) { GcalRevokedBanner() }
        .changesetNoticeToast()
        .sheet(isPresented: $navigation.isReviewingProposal) {
            ProposalReviewSheet()
        }
    }
}
