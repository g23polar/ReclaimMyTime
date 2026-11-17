import Foundation
import Observation
import OSLog
import SwiftUI
import UserNotifications

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
    /// The Projects tab's list (`GET /projects`). Separate from `projects`, which caches cards.
    let projectList: ProjectsModel
    /// Local notifications: the 48-hour window, warnings setting, permission pre-prompt (T5.4).
    let notifications: NotificationScheduler
    /// The time-zone change prompt (T5.4).
    let timezone: TimezoneMonitor

    static let lastDriftNotifiedKey = "drift.lastNotifiedChangeset"

    private let defaults: UserDefaults
    private let backgroundScheduler: any BackgroundRefreshScheduling
    private let driftSource: any DriftSource
    /// The `POST /gcal/sync` in flight, shared by concurrent callers (foreground, background, tap).
    private var syncTask: Task<DriftCheckResult?, Never>?
    /// Bumped by `resetSessionState()` (sign-out). With the bearer it identifies the session a
    /// sync, foreground or background pass started in; side effects for an older one are dropped,
    /// even if the same user signed back in meanwhile.
    private var sessionGeneration = 0
    /// Whether the scene is in the foreground (`.active`/`.inactive`), so a sign-in that lands
    /// after activation (cold start before bootstrap finished) still runs the foreground pass.
    private var sceneIsActive = false
    /// The session generation the foreground pass last ran for (de-dupes auth + scene triggers).
    private var foregroundRanForGeneration: Int?
    /// Retained here: `UNUserNotificationCenter.delegate` is weak.
    private var responder: NotificationResponder?
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "app")

    /// - Parameters:
    ///   - transport: override for tests; defaults to the fixture mock or URLSession per `config`.
    ///   - sessionStore: override for tests; defaults to a Keychain slot keyed by backend.
    init(
        config: AppConfig = .fromProcess(),
        transport: (any HTTPTransport)? = nil,
        sessionStore: SessionStore? = nil,
        defaults: UserDefaults = .standard,
        notificationCenter: (any LocalNotificationCenter)? = nil,
        backgroundScheduler: (any BackgroundRefreshScheduling)? = nil,
        driftSource: (any DriftSource)? = nil
    ) {
        self.config = config
        self.defaults = defaults
        self.backgroundScheduler = backgroundScheduler ?? SystemBackgroundRefreshScheduler()
        let transport: any HTTPTransport = transport ?? Self.defaultTransport(config: config)
        let sessionStore = sessionStore ?? SessionStore.forBackend(config.backend)
        let relay = SessionInvalidationRelay()
        let gcalRelay = GcalReconnectRelay()
        let api = URLSessionAPIClient(
            baseURL: config.baseURL,
            transport: transport,
            sessionStore: sessionStore,
            onUnauthenticated: { token in await relay.notify(token: token) },
            onGcalReconnectRequired: { token in await gcalRelay.notify(token: token) }
        )
        self.api = api
        self.driftSource = driftSource ?? GcalSyncDriftSource(api: api)
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
        let calendar = CalendarConnectionModel(api: api, defaults: defaults, scope: SessionStore.service(for: config.backend, prefix: "gcal"))
        self.calendar = calendar
        gcalRelay.calendar = calendar
        gcalRelay.auth = auth
        let chat = ChatModel(api: api, pendingStore: pending, navigation: navigation, directory: projects)
        self.chat = chat
        let timetable = TimetableModel(api: api, pending: pending, navigation: navigation, directory: projects)
        self.timetable = timetable
        self.projectList = ProjectsModel(api: api)
        let notifications = NotificationScheduler(center: notificationCenter ?? SystemNotificationCenter(), defaults: defaults)
        self.notifications = notifications
        let timezone = TimezoneMonitor(api: api, pending: pending, defaults: defaults)
        self.timezone = timezone
        relay.auth = auth
        // Before the phase flips: RootView's consent routing must already see the grant.
        auth.onGoogleSignIn = { [weak calendar] session, gcalConnected in
            calendar?.adoptSignInGrant(userId: session.userId, gcalConnected: gcalConnected)
        }
        projects.onProjectRead = { [weak timetable] project in
            timetable?.adoptProject(project)
        }

        pending.onConfirmed = { [weak chat, weak projects, weak timetable, weak notifications] changeset in
            projects?.applyConfirmed(changeset)
            timetable?.noteConfirmed(changeset)
            chat?.changesetConfirmed(changeset)
            // Permission is asked after a week is confirmed, never at launch.
            if changeset.planId != nil, let notifications {
                Task { await notifications.weekConfirmed() }
            }
        }
        pending.onDiscarded = { [weak chat, weak palette, weak timezone] changeset, replacement in
            chat?.changesetDiscarded(changeset, replacement: replacement)
            timezone?.changesetDiscarded(changeset, replacement: replacement)
            // A staged project that never got confirmed gives its palette slot back.
            let kept = Set(replacement.map(Self.createdProjectIds) ?? [])
            for id in Self.createdProjectIds(changeset) where !kept.contains(id) {
                palette?.release(id)
            }
        }
        pending.onStale = { [weak self] _ in
            // The fresh proposal is only reachable through sync (D25); its stage() replaces this one.
            // A fresh request: a sync already in flight predates the stale answer.
            Task { await self?.syncGcal(forceNew: true) }
        }
        wirePlanObservers()
    }

    // MARK: notifications, foreground, background (T5.4)

    /// Called once from `App.init`, before launch finishes, so a notification tap that launches
    /// the app is delivered. Not called by tests.
    func installSystemHooks() {
        let responder = NotificationResponder { [weak self] route in
            await self?.handle(route)
        }
        self.responder = responder
        UNUserNotificationCenter.current().delegate = responder
        Task { await notifications.registerCategories() }
    }

    /// Wires plan publication to the notification window and the time-zone check. Separate from
    /// `installSystemHooks` so tests get it without touching the system delegate.
    private func wirePlanObservers() {
        timetable.onPlanChanged = { [weak self] plan in self?.planChanged(plan) }
    }

    private func planChanged(_ plan: WeekPlan?) {
        notifications.enqueueRebuild(notificationSnapshot())
        timezone.check(plan: plan)
    }

    /// What the window is built from: the live plan plus names/pins/projects for the copy.
    func notificationSnapshot() -> NotificationSnapshot {
        let plan = timetable.plan
        var names: [UUID: String] = [:]
        for id in Set((plan?.blocks ?? []).map(\.projectId)) {
            names[id] = timetable.projectName(id)
        }
        return NotificationSnapshot(plan: plan, pins: timetable.pins, projects: timetable.projects, projectNames: names)
    }

    func handleScenePhase(_ phase: ScenePhase) {
        switch phase {
        case .active:
            sceneIsActive = true
            Task { await handleForeground() }
        case .background:
            sceneIsActive = false
            scheduleBackgroundRefresh()
        default:
            break
        }
    }

    /// Every foreground: sync with Google (D25) alongside refreshing the week (which rebuilds the
    /// window on publish), rebuild anyway (offline keeps the last plan, and the 48-hour window has
    /// moved), compare time zones, and re-read the Google connection (§5). The sync runs
    /// concurrently: its drain can take ~10 s and must never hold up the timetable. A successful
    /// sync reloads the week itself; that load takes a newer ticket, so this earlier one can't
    /// overwrite it even if it finishes last.
    func handleForeground() async {
        guard case .signedIn = auth.phase else { return }
        foregroundRanForGeneration = sessionGeneration
        async let synced = syncGcal()
        await notifications.refreshAuthorization()
        await timetable.load()
        await notifications.rebuild(notificationSnapshot())
        timezone.check(plan: timetable.plan)
        await calendar.refreshStatus()
        _ = await synced
    }

    /// `POST /gcal/sync` (D25): drains the write-back outbox and recovers the pending changeset
    /// (any reason) into the store, where the banner shows it. Never applies anything.
    /// - 409 `gcal_reconnect_required`: the reconnect banner, once per revocation.
    /// - 503 `gcal_unavailable` (and any other failure): silent; the next foreground retries.
    /// - 200: reload the live plan and rebuild reminders, whatever `pending_changeset` is: sync may
    ///   have applied a Google-side move or delete directly (T4.3b) with no proposal at all.
    /// Concurrent callers share one request unless `forceNew` (a caller that needs an answer newer
    /// than anything in flight). Returns nil on failure or if the account changed.
    @discardableResult
    func syncGcal(forceNew: Bool = false) async -> DriftCheckResult? {
        if !forceNew, let syncTask { return await syncTask.value }
        let task = Task { await self.performSync() }
        syncTask = task
        let result = await task.value
        if syncTask == task { syncTask = nil }
        return result
    }

    private func performSync() async -> DriftCheckResult? {
        guard let epoch = currentEpoch() else { return nil }
        // Anything adopted after this point is newer than the server's answer.
        let stagedBefore = pending.stagingSequence
        do {
            let result = try await driftSource.check()
            guard isCurrent(epoch) else { return nil }
            calendar.noteSyncSucceeded()
            if let changeset = result.pendingChangeset {
                projects.register(changeset)
                pending.stage(changeset)
            } else {
                // Nothing pending server-side: a proposal held since before this request is dead
                // (confirmed, rejected or expired elsewhere). One adopted since is kept.
                pending.retireIfUnchanged(since: stagedBefore)
            }
            // Issued after the sync answered, so its ticket beats any load started before it.
            await timetable.load()
            guard isCurrent(epoch) else { return nil }
            await notifications.rebuild(notificationSnapshot())
            guard isCurrent(epoch) else { return nil }
            return result
        } catch {
            guard isCurrent(epoch) else { return nil }
            switch GcalSolveFailure(error) {
            case .reconnectRequired?:
                calendar.noteReconnectRequired(.oncePerRevocation)
            case .unavailable?:
                logger.notice("Google sync unavailable; retrying on the next foreground")
            case nil:
                logger.notice("Google sync failed")
            }
            return nil
        }
    }

    /// The session a pass started in: generation + bearer.
    private struct SessionEpoch: Equatable {
        let generation: Int
        let token: String
    }

    private func currentEpoch() -> SessionEpoch? {
        guard case .signedIn(let session) = auth.phase else { return nil }
        return SessionEpoch(generation: sessionGeneration, token: session.token)
    }

    private func isCurrent(_ epoch: SessionEpoch) -> Bool {
        currentEpoch() == epoch
    }

    /// Asks for the next `BGAppRefreshTask` (on entering the background, and from the task itself).
    func scheduleBackgroundRefresh() {
        guard case .signedIn = auth.phase else { return }
        do {
            try backgroundScheduler.submitAppRefresh(
                identifier: BackgroundTaskID.driftCheck,
                earliestBeginDate: timetable.now().addingTimeInterval(BackgroundTaskID.refreshInterval)
            )
        } catch {
            // The simulator and Low Power Mode refuse; the next foreground covers it.
            logger.notice("Background refresh not scheduled")
        }
    }

    /// The `BGAppRefreshTask` handler: sync with Google first (it may apply Google-side edits),
    /// then the live plan and the reminder window (a successful sync reloads both; a failed one
    /// still refetches so the window keeps rolling), and, if the pending changeset is a new
    /// `drift` or `google_edit` proposal, post the §4 notification once. Never applies it.
    func handleBackgroundRefresh() async {
        if case .loading = auth.phase { await auth.bootstrap() }
        guard let epoch = currentEpoch() else { return }
        scheduleBackgroundRefresh()
        let result = await syncGcal()
        guard isCurrent(epoch) else { return }
        if result == nil {
            await timetable.load()
            guard isCurrent(epoch) else { return }
            await notifications.rebuild(notificationSnapshot())
        }
        guard isCurrent(epoch),
              let alert = result?.alert,
              defaults.string(forKey: Self.lastDriftNotifiedKey) != alert.changesetId.pathComponent
        else { return }
        defaults.set(alert.changesetId.pathComponent, forKey: Self.lastDriftNotifiedKey)
        let request = DriftNotification.request(alert, projectName: alert.projectId.map(timetable.projectName) ?? "", time: timetable.timeText)
        await notifications.post(request)
    }

    /// A notification response (tap or action), on the main actor.
    func handle(_ route: NotificationRoute) async {
        if case .loading = auth.phase { await auth.bootstrap() }
        guard case .signedIn = auth.phase else { return }
        switch route {
        case let .openBlock(lineageId, _):
            navigation.selectedTab = .week
            if timetable.plan == nil { await timetable.load() }
            if let block = timetable.plan?.blocks.first(where: { $0.lineageId == lineageId }) {
                timetable.requestedDay = timetable.calendar.startOfDay(for: block.span.start)
            }
        case let .skipBlock(lineageId, blockId):
            await timetable.skip(lineageId: lineageId, fallbackBlockId: blockId)
        case .drift(let changesetId):
            navigation.selectedTab = .week
            // Cold start (or a newer proposal since): recover it through sync (O2, D25).
            if !pending.hasPending || (changesetId != nil && pending.pending?.id != changesetId) {
                await syncGcal()
            }
            if pending.hasPending { navigation.presentReview() }
        }
    }

    /// Routes auth phase changes to per-account state (RootView calls this on every change,
    /// including the initial one).
    func handleAuthPhase(_ phase: AuthState.Phase) {
        switch phase {
        case .signedIn(let session):
            calendar.activate(userId: session.userId)
            // Activated before bootstrap finished (cold start) or a fresh sign-in while open: run
            // the foreground pass once for this session. A scene-phase pass that already ran is
            // not repeated; a concurrent one shares the sync request.
            if sceneIsActive, foregroundRanForGeneration != sessionGeneration {
                foregroundRanForGeneration = sessionGeneration
                Task { await handleForeground() }
            }
        case .signedOut:
            resetSessionState()
        case .loading, .sessionUnavailable:
            break
        }
    }

    /// Clears per-user in-memory state on sign-out.
    func resetSessionState() {
        sessionGeneration += 1
        syncTask?.cancel()
        syncTask = nil
        chat.reset()
        pending.reset()
        projects.reset()
        calendar.activate(userId: nil)
        timetable.reset()
        projectList.reset()
        timezone.reset()
        notifications.signOut()
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
        var variants: [String: String] = [:]
        #if DEBUG
        // `-MockGcalSync <case>` serves `post_gcal_sync__<case>.json` for every sync, e.g.
        // `pending_drift` (a drift proposal: banner in the foreground, notification from a
        // background refresh), `reconnect_required`, `unavailable`.
        let arguments = ProcessInfo.processInfo.arguments
        if let flag = arguments.firstIndex(of: "-MockGcalSync"), arguments.indices.contains(flag + 1) {
            variants["syncGcal"] = arguments[flag + 1]
        }
        #endif
        let mock = MockTransport.bundled(variants: variants)
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
            await auth.signInWithGoogle(using: { url, _ in MockWebAuthenticator.signInCallbackURL(authorizationURL: url, arguments: []) })
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

/// Routes the client's 409 `gcal_reconnect_required` (any solving route, HTTP or in-stream) to the
/// reconnect banner. A refused change always shows it; `/gcal/sync` opts out and announces itself.
/// Like the 401 path, a late answer for a session that is no longer current is ignored, so it can
/// never mark another account revoked.
@MainActor
final class GcalReconnectRelay {
    weak var calendar: CalendarConnectionModel?
    weak var auth: AuthState?

    func notify(token: String?) {
        guard let token, case .signedIn(let session)? = auth?.phase, session.token == token else { return }
        calendar?.noteReconnectRequired(.always)
    }
}

/// Breaks the client ↔ auth construction cycle without a strong reference.
@MainActor
final class SessionInvalidationRelay {
    weak var auth: AuthState?

    func notify(token: String?) {
        auth?.sessionInvalidated(token: token)
    }
}
