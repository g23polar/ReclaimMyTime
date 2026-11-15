import Foundation
import Observation

/// Composition root: builds the transport (mock or network), the typed client, the session store
/// and auth state, and the app-wide observable state (pending changeset, chat, calendar, tabs).
@MainActor
@Observable
final class AppModel {
    let config: AppConfig
    let api: any APIClient
    let auth: AuthState
    let palette: ProjectPaletteStore
    let navigation: AppNavigation
    /// The single pending changeset (T5.3 builds review on this).
    let pending: PendingChangesetStore
    let projects: ProjectDirectory
    let calendar: CalendarConnectionModel
    let chat: ChatModel

    /// - Parameters:
    ///   - transport: override for tests; defaults to the fixture mock or URLSession per `config`.
    ///   - sessionStore: override for tests; defaults to a Keychain slot keyed by backend.
    init(
        config: AppConfig = .fromProcess(),
        transport: (any HTTPTransport)? = nil,
        sessionStore: SessionStore? = nil,
        defaults: UserDefaults = .standard
    ) {
        self.config = config
        let transport: any HTTPTransport = transport ?? (config.usesMock ? MockTransport.bundled() : URLSessionTransport())
        let sessionStore = sessionStore ?? SessionStore.forBackend(config.backend)
        let relay = SessionInvalidationRelay()
        let api = URLSessionAPIClient(
            baseURL: config.baseURL,
            transport: transport,
            sessionStore: sessionStore,
            onUnauthenticated: { token in await relay.notify(token: token) }
        )
        self.api = api
        let auth = AuthState(api: api, sessionStore: sessionStore)
        self.auth = auth
        let palette = ProjectPaletteStore(defaults: defaults)
        self.palette = palette
        let navigation = AppNavigation()
        self.navigation = navigation
        let pending = PendingChangesetStore(api: api)
        self.pending = pending
        let projects = ProjectDirectory(api: api)
        self.projects = projects
        calendar = CalendarConnectionModel(api: api, defaults: defaults)
        let chat = ChatModel(api: api, pendingStore: pending, navigation: navigation, directory: projects)
        self.chat = chat
        relay.auth = auth

        pending.onConfirmed = { [weak chat, weak projects] changeset in
            projects?.applyConfirmed(changeset)
            chat?.changesetConfirmed(changeset)
        }
        pending.onDiscarded = { [weak chat, weak palette] changeset, replacement in
            chat?.changesetDiscarded(changeset)
            // A staged project that never got confirmed gives its palette slot back.
            let kept = Set(replacement.map(Self.createdProjectIds) ?? [])
            for id in Self.createdProjectIds(changeset) where !kept.contains(id) {
                palette?.release(id)
            }
        }
    }

    /// Clears per-user in-memory state on sign-out.
    func resetSessionState() {
        chat.reset()
        pending.reset()
        projects.reset()
        navigation.selectedTab = .week
    }

    private static func createdProjectIds(_ changeset: Changeset) -> [UUID] {
        changeset.staged.compactMap { mutation in
            if case let .projectCreated(id, _) = mutation.change { return id }
            return nil
        }
    }

    #if DEBUG
    /// Mock-only demo states for screenshots/manual QA: `-DemoScenario cards|preferences`. Signs in
    /// with the mock account, skips consent, opens Chat, and attaches the onboarding fixture's
    /// staged project (`post_projects__perennial_onboarding`). No agent call is made.
    func runDemoScenarioIfRequested(arguments: [String] = ProcessInfo.processInfo.arguments) async {
        guard config.usesMock, let flag = arguments.firstIndex(of: "-DemoScenario"), arguments.indices.contains(flag + 1) else { return }
        let scenario = arguments[flag + 1]
        if case .signedIn = auth.phase {} else {
            await auth.signInWithApple(identityToken: "mock-identity-token", authorizationCode: "mock-authorization-code", rawNonce: Nonce.random())
        }
        guard case .signedIn(let session) = auth.phase else { return }
        calendar.completeConsentStep(userId: session.userId)
        navigation.selectedTab = .chat
        guard let changeset = try? await api.createProject(ProjectDraft(name: "Reading", goal: .perennial(weeklyTargetHours: 2), maxBlockMinutes: 60, minBlockMinutes: 25, requirements: [])) else { return }
        chat.debugAttach(changeset: changeset, text: changeset.narration ?? "Here's what I've got.")
        if scenario == "preferences" {
            await chat.confirmCards(entryId: chat.entries.last?.id ?? UUID())
        }
    }
    #endif
}

/// Breaks the client ↔ auth construction cycle without a strong reference.
@MainActor
final class SessionInvalidationRelay {
    weak var auth: AuthState?

    func notify(token: String?) {
        auth?.sessionInvalidated(token: token)
    }
}
