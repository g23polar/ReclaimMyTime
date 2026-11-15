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
    let timetable: TimetableModel

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
        let transport: any HTTPTransport = transport ?? Self.defaultTransport(config: config)
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
        calendar = CalendarConnectionModel(api: api, defaults: defaults, scope: SessionStore.service(for: config.backend, prefix: "gcal"))
        let chat = ChatModel(api: api, pendingStore: pending, navigation: navigation, directory: projects)
        self.chat = chat
        let timetable = TimetableModel(api: api, pending: pending, navigation: navigation, directory: projects)
        self.timetable = timetable
        relay.auth = auth

        pending.onConfirmed = { [weak chat, weak projects, weak timetable] changeset in
            projects?.applyConfirmed(changeset)
            timetable?.noteConfirmed(changeset)
            chat?.changesetConfirmed(changeset)
        }
        pending.onDiscarded = { [weak chat, weak palette] changeset, replacement in
            chat?.changesetDiscarded(changeset, replacement: replacement)
            // A staged project that never got confirmed gives its palette slot back.
            let kept = Set(replacement.map(Self.createdProjectIds) ?? [])
            for id in Self.createdProjectIds(changeset) where !kept.contains(id) {
                palette?.release(id)
            }
        }
    }

    /// Routes auth phase changes to per-account state (RootView calls this on every change,
    /// including the initial one).
    func handleAuthPhase(_ phase: AuthState.Phase) {
        switch phase {
        case .signedIn(let session):
            calendar.activate(userId: session.userId)
        case .signedOut:
            resetSessionState()
        case .loading, .sessionUnavailable:
            break
        }
    }

    /// Clears per-user in-memory state on sign-out.
    func resetSessionState() {
        chat.reset()
        pending.reset()
        projects.reset()
        calendar.activate(userId: nil)
        timetable.reset()
        navigation.isReviewingProposal = false
        navigation.tradeOffsRequested = false
        navigation.selectedTab = .week
    }

    private static func createdProjectIds(_ changeset: Changeset) -> [UUID] {
        changeset.staged.compactMap { mutation in
            if case let .projectCreated(id, _) = mutation.change { return id }
            return nil
        }
    }

    private static func defaultTransport(config: AppConfig) -> any HTTPTransport {
        guard config.usesMock else { return URLSessionTransport() }
        let mock = MockTransport.bundled()
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-DemoScenario") { return DemoTransport(inner: mock) }
        #endif
        return mock
    }

    #if DEBUG
    /// Mock-only demo states for screenshots/manual QA, `-DemoScenario <name>`:
    /// - `cards` / `preferences`: onboarding intake (Chat) from `post_projects__perennial_onboarding`.
    /// - `week` / `day`: the live fixture week at the story's "now" (Tue 10:30 LA); `day` opens Wed,
    ///   whose Workout block is pinned by `DemoTransport`.
    /// - `review`: a drag's staged pin (`patch_blocks_id__edit_pin`) with review open.
    /// - `tradeoff`: the infeasible Grant proposal (`post_projects__deadline_infeasible`) with the
    ///   trade-off sheet open.
    /// Signs in with the mock account and skips consent. No agent call is made.
    func runDemoScenarioIfRequested(arguments: [String] = ProcessInfo.processInfo.arguments) async {
        guard config.usesMock, let flag = arguments.firstIndex(of: "-DemoScenario"), arguments.indices.contains(flag + 1) else { return }
        let scenario = arguments[flag + 1]
        if case .signedIn = auth.phase {} else {
            await auth.signInWithApple(identityToken: "mock-identity-token", authorizationCode: "mock-authorization-code", rawNonce: Nonce.random())
        }
        guard case .signedIn(let session) = auth.phase else { return }
        calendar.activate(userId: session.userId)
        calendar.completeConsentStep(userId: session.userId)
        switch scenario {
        case "week", "day", "review", "tradeoff":
            await runTimetableDemo(scenario)
        default:
            navigation.selectedTab = .chat
            guard let changeset = try? await api.createProject(ProjectDraft(name: "Reading", goal: .perennial(weeklyTargetHours: 2), maxBlockMinutes: 60, minBlockMinutes: 25, requirements: [])) else { return }
            chat.debugAttach(changeset: changeset, text: changeset.narration ?? "Here's what I've got.")
            if scenario == "preferences" {
                await chat.confirmCards(entryId: chat.entries.last?.id ?? UUID())
            }
        }
    }

    private func runTimetableDemo(_ scenario: String) async {
        let now = DemoTransport.demoNow
        timetable.now = { now }
        navigation.selectedTab = .week
        await timetable.load()
        switch scenario {
        case "day":
            // Wed 18 Nov (display calendar): the pinned Workout block's day.
            timetable.requestedDay = timetable.calendar.startOfDay(for: now.addingTimeInterval(36 * 3600))
        case "review":
            guard let block = timetable.plan?.blocks.first(where: { $0.span.start > now }),
                  let span = Span(start: block.span.start.addingTimeInterval(-3600), end: block.span.end.addingTimeInterval(-3600)),
                  case .staged(let changeset)? = try? await api.patchBlock(id: block.id, action: .edit(span: span))
            else { return }
            projects.register(changeset)
            if pending.stage(changeset) { navigation.presentReview() }
        case "tradeoff":
            let draft = ProjectDraft(name: "Grant application", goal: .deadline(targetDate: LocalDate("2026-11-20")!, estimatedTotalHours: 20), maxBlockMinutes: 120, minBlockMinutes: 30, requirements: ["laptop"])
            guard let demo = (try? await debugCreateInfeasibleProject(draft)) else { return }
            projects.register(demo)
            if pending.stage(demo) {
                navigation.tradeOffsRequested = true
                navigation.presentReview()
            }
        default:
            break
        }
    }

    /// The fixture mock serves `perennial_onboarding` for `POST /projects` by default; the demo
    /// needs the `deadline_infeasible` case, decoded from the bundled fixture.
    private func debugCreateInfeasibleProject(_ draft: ProjectDraft) async throws -> Changeset? {
        guard let directory = FixtureLoader.bundledDirectory() else { return nil }
        let fixture = try FixtureLoader.parse(directory.appending(path: "post_projects__deadline_infeasible.json"))
        guard let body = fixture.responseBody else { return nil }
        return try JSONCoding.makeDecoder().decode(Changeset.self, from: body)
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
