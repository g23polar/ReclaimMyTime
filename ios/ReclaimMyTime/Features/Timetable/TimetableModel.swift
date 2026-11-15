import Foundation
import Observation
import OSLog

/// The live week (timetable.md): `GET /plans/current`, `/pins`, `/projects`, `/preferences`,
/// `/exceptions`. Draws what the server says; edits go out as staged writes (`PATCH /blocks`) whose
/// 202 changesets land in `PendingChangesetStore`. Nothing is placed or applied client-side.
@MainActor
@Observable
final class TimetableModel {
    enum LoadPhase: Hashable, Sendable {
        case idle
        case loading
        case loaded
        /// 404 `no_live_plan`.
        case noPlan
        /// The first fetch failed and there is no earlier plan to show.
        case unavailable
    }

    enum Connectivity: Hashable, Sendable {
        case online
        /// Transport failure: the last plan stays visible.
        case offline
        /// Server error: the last plan stays visible.
        case error
    }

    /// Transient messages (toast/callout).
    enum Notice: Hashable, Sendable, Identifiable {
        case weekChanged
        case pastDrop
        case notStarted
        case unreachable
        case failed
        case syncFailed

        var id: Self { self }

        var message: String {
            switch self {
            case .weekChanged: "Your week changed. Try that again."
            case .pastDrop: "You can't move things into the past."
            case .notStarted: "That block hasn't started yet."
            case .unreachable: "Couldn't reach the server. Nothing changed; try again."
            case .failed: "That didn't work. Nothing changed."
            case .syncFailed: "This block didn't reach Google Calendar yet. We'll keep trying."
            }
        }
    }

    enum RelaxationOutcome: Hashable, Sendable {
        case staged
        /// A newer proposal already exists; this one was not adopted.
        case ignored
        case failed(String)
    }

    private(set) var phase: LoadPhase = .idle
    private(set) var plan: WeekPlan?
    private(set) var pins: [BlockPin] = []
    private(set) var projects: [UUID: Project] = [:]
    private(set) var preferences: UserPreferences?
    private(set) var exceptions: [AvailabilityException] = []
    private(set) var connectivity: Connectivity = .online
    private(set) var notice: Notice?
    /// Edits sent but not yet answered: drawn as proposals at once.
    private(set) var inFlight: [UUID: StagedBlockEdit] = [:]
    /// Lineages that changed in the last confirmed changeset: pulse once on the new live plan.
    private(set) var pulseLineages: Set<UUID> = []
    /// Bumped on every rejected drop (drives the rigid haptic).
    private(set) var rejectedDropCount = 0
    /// A day the Week tab should open (demo hooks, notification taps).
    var requestedDay: Date?
    /// Minute tick counter; see `advanceClock()`.
    private(set) var clock = 0
    /// Called whenever the published plan changes (including to nil), after reference data that
    /// arrived with it. Drives the local-notification rebuild (T5.4).
    var onPlanChanged: (@MainActor (WeekPlan?) -> Void)?

    /// Design gap G1: no route exposes calendar busy intervals, so this stays nil and the busy
    /// layer is not drawn. TODO(G1): fill from `WeekPlan.busy` if the contract adds it.
    let busy: [Span]? = nil

    /// The device clock; injectable for tests and demo screenshots.
    var now: @MainActor () -> Date = { Date() }
    /// Display calendar (device time zone).
    var calendar: Calendar = .autoupdatingCurrent

    private let api: any APIClient
    private let pending: PendingChangesetStore
    private let navigation: AppNavigation
    private let directory: ProjectDirectory
    private var generation = 0
    /// Monotonic load tickets. Every load takes one; a mutation that publishes newer state
    /// (complete/skip, confirm, a project read) takes one too. Each resource keeps the ticket of
    /// what it last published and drops any result carrying an older ticket.
    private var ticketCounter = 0
    private var publishedTickets: [Resource: Int] = [:]
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "timetable")

    init(api: any APIClient, pending: PendingChangesetStore, navigation: AppNavigation, directory: ProjectDirectory) {
        self.api = api
        self.pending = pending
        self.navigation = navigation
        self.directory = directory
    }

    // MARK: derived

    var pinnedBlockIds: Set<UUID> {
        PinJoin.pinnedBlockIds(blocks: plan?.blocks ?? [], pins: pins)
    }

    var forbids: [BlockPin] { PinJoin.forbids(pins) }

    func classification(for block: Block) -> BlockClassification {
        _ = clock
        return BlockClassifier.classify(block, now: now(), pinnedIds: pinnedBlockIds)
    }

    /// Staged edits from the pending changeset plus edits still in flight.
    var stagedEdits: [UUID: StagedBlockEdit] {
        StagedEdits.from(pending.pending).merging(inFlight) { _, local in local }
    }

    /// Time zone the schedulable windows are written in (preferences, else the plan's).
    var windowTimeZone: TimeZone? {
        (preferences?.timezone ?? plan?.timezone).flatMap(TimeZone.init(identifier:))
    }

    /// Project name from what this client knows: live projects, staged creations it has seen
    /// (D23: staged projects are only visible inside their changeset), the pending changeset.
    func projectName(_ id: UUID) -> String {
        if let project = projects[id] { return project.name }
        if let name = directory.card(for: id)?.name { return name }
        for mutation in pending.pending?.staged ?? [] {
            if case let .projectCreated(projectId, draft) = mutation.change, projectId == id { return draft.name }
        }
        return "Project"
    }

    /// Start of the week shown at `offset` weeks from the current one.
    func weekStart(offset: Int) -> Date {
        let base: Date
        if let plan {
            base = calendar.startOfDay(for: plan.horizon.start)
        } else {
            base = TimetableGeometry.weekStart(containing: now(), isoWeekStart: preferences?.weekStart ?? 1, calendar: calendar)
        }
        return calendar.date(byAdding: .day, value: 7 * offset, to: base) ?? base
    }

    /// Columns for the week at `offset`. The live week is drawn from the plan's own horizon, split
    /// into the local days it touches (7 normally; 8 when the device zone differs from the plan's),
    /// so every block has a column. Other weeks are 7 device days placed around it.
    func columns(weekOffset: Int) -> [DayColumn] {
        guard let plan else {
            return TimetableGeometry.columns(from: weekStart(offset: weekOffset), calendar: calendar)
        }
        let current = TimetableGeometry.columns(spanning: plan.horizon, calendar: calendar)
        guard weekOffset != 0, let first = current.first, let last = current.last else { return current }
        let anchor = weekOffset > 0
            ? calendar.date(byAdding: .day, value: 7 * (weekOffset - 1), to: last.end)
            : calendar.date(byAdding: .day, value: 7 * weekOffset, to: first.start)
        return TimetableGeometry.columns(from: anchor ?? first.start, calendar: calendar)
    }

    /// The minute tick: time-dependent presentation (classification, elapsed shading, which
    /// actions a block offers) reads `clock`, so bumping it re-renders those views.
    func advanceClock() {
        clock &+= 1
    }

    /// Bumps `clock` on every minute boundary until cancelled (the Week tab's `.task`).
    func runClock() async {
        while !Task.isCancelled {
            let now = Date().timeIntervalSinceReferenceDate
            let next = (now / 60).rounded(.down) * 60 + 60
            try? await Task.sleep(for: .seconds(next - now))
            guard !Task.isCancelled else { return }
            advanceClock()
        }
    }

    /// Blocks shown for a week: the live plan only covers the current one (O7: no plan history).
    func blocks(weekOffset: Int) -> [Block] {
        weekOffset == 0 ? plan?.blocks ?? [] : []
    }

    func tradeOffContext(for changeset: Changeset?) -> TradeOffContext {
        var context = TradeOffContext()
        var projectIds = Set(projects.keys)
        for block in (plan?.blocks ?? []) + (changeset?.plan?.blocks ?? []) {
            context.lineageProjects[block.lineageId] = block.projectId
            projectIds.insert(block.projectId)
        }
        for entry in changeset?.diff?.entries ?? [] {
            context.lineageProjects[entry.lineageId] = entry.projectId
            projectIds.insert(entry.projectId)
        }
        if let infeasibility = changeset?.plan?.infeasibility {
            for violation in infeasibility.violations {
                switch violation {
                case .demandShortfall(let value): projectIds.insert(value.projectId)
                case .pinInvalidated(let value):
                    if let id = value.projectId { projectIds.insert(id) }
                    context.pins[value.pinId] = .init(projectId: value.projectId, span: value.span)
                }
            }
            for relaxation in infeasibility.relaxations {
                switch relaxation {
                case let .extendDeadline(id, _, _), let .reduceEstimate(id, _, _), let .reduceWeeklyTarget(id, _, _),
                     let .raiseMaxBlockMinutes(id, _, _), let .pauseProject(id, _):
                    projectIds.insert(id)
                case .removePin, .removeException:
                    break
                }
            }
        }
        for mutation in changeset?.staged ?? [] {
            switch mutation.change {
            case let .projectCreated(id, draft):
                context.projectNames[id] = draft.name
            case .exceptionAdded(let payload):
                context.exceptionSpans[payload.exceptionId] = payload.span
                if let reason = payload.reason { context.exceptionReasons[payload.exceptionId] = reason }
            case .pinAdded(let payload):
                context.pins[payload.pinId] = .init(projectId: payload.projectId, span: payload.span)
                if let id = payload.projectId { projectIds.insert(id) }
            default:
                break
            }
        }
        for pin in pins where context.pins[pin.id] == nil {
            context.pins[pin.id] = .init(projectId: pin.projectId, span: pin.span)
            if let id = pin.projectId { projectIds.insert(id) }
        }
        for exception in exceptions {
            context.exceptionSpans[exception.id] = exception.span
            if let reason = exception.reason { context.exceptionReasons[exception.id] = reason }
        }
        for id in projectIds where context.projectNames[id] == nil {
            let name = projectName(id)
            if name != "Project" { context.projectNames[id] = name }
        }
        return context
    }

    // MARK: loading

    private enum Fetch<Value: Sendable>: Sendable {
        case ok(Value)
        case failed(APIError)
        case cancelled
    }

    private nonisolated static func fetch<Value: Sendable>(_ operation: @Sendable () async throws -> Value) async -> Fetch<Value> {
        do {
            return .ok(try await operation())
        } catch let error as APIError {
            return .failed(error)
        } catch {
            return .cancelled
        }
    }

    private enum Resource: Hashable {
        case plan, pins, projects, preferences, exceptions
    }

    private enum LoadResult: Sendable {
        case plan(Fetch<WeekPlan>)
        case pins(Fetch<[BlockPin]>)
        case projects(Fetch<[Project]>)
        case preferences(Fetch<UserPreferences>)
        case exceptions(Fetch<[AvailabilityException]>)
    }

    private func issueTicket() -> Int {
        ticketCounter += 1
        return ticketCounter
    }

    /// True (and recorded) when `ticket` is newer than what `resource` last published.
    private func accept(_ resource: Resource, ticket: Int) -> Bool {
        guard ticket > publishedTickets[resource, default: 0] else { return false }
        publishedTickets[resource] = ticket
        return true
    }

    private func isCurrent(_ resource: Resource, ticket: Int) -> Bool {
        ticket > publishedTickets[resource, default: 0]
    }

    /// Fetches the week and its reference data. Each part publishes as soon as it arrives (the
    /// plan never waits on pins/projects/preferences/exceptions, which enrich it later), and only
    /// if nothing newer was published for that part since this load began. Reference data keeps
    /// its last value on failure.
    func load() async {
        let started = generation
        let ticket = issueTicket()
        if plan == nil, phase != .noPlan { phase = .loading }
        let api = self.api
        await withTaskGroup(of: LoadResult.self) { group in
            group.addTask { .plan(await Self.fetch { try await api.getCurrentPlan() }) }
            group.addTask { .pins(await Self.fetch { try await api.listPins() }) }
            group.addTask { .projects(await Self.fetch { try await api.listProjects() }) }
            group.addTask { .preferences(await Self.fetch { try await api.getPreferences() }) }
            group.addTask { .exceptions(await Self.fetch { try await api.listExceptions() }) }
            for await result in group {
                guard started == generation else { continue }
                publish(result, ticket: ticket)
            }
        }
    }

    private func publish(_ result: LoadResult, ticket: Int) {
        switch result {
        case .pins(.ok(let value)):
            if accept(.pins, ticket: ticket) {
                pins = value
                notifyPlanChanged()
            }
        case .projects(.ok(let value)):
            if accept(.projects, ticket: ticket) {
                projects = Dictionary(value.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
                notifyPlanChanged()
            }
        case .preferences(.ok(let value)):
            if accept(.preferences, ticket: ticket) { preferences = value }
        case .exceptions(.ok(let value)):
            if accept(.exceptions, ticket: ticket) { exceptions = value }
        case .pins, .projects, .preferences, .exceptions:
            break
        case .plan(let fetch):
            publishPlan(fetch, ticket: ticket)
        }
    }

    private func publishPlan(_ fetch: Fetch<WeekPlan>, ticket: Int) {
        switch fetch {
        case .ok(let value):
            guard accept(.plan, ticket: ticket) else { return }
            plan = value
            phase = .loaded
            connectivity = .online
            notifyPlanChanged()
        case .failed(let error) where error.serverCode == KnownErrorCode.noLivePlan:
            guard accept(.plan, ticket: ticket) else { return }
            plan = nil
            phase = .noPlan
            connectivity = .online
            notifyPlanChanged()
        case .failed(let error):
            guard isCurrent(.plan, ticket: ticket) else { return }
            if case .transport = error { connectivity = .offline } else { connectivity = .error }
            logger.notice("Plan refresh failed")
            phase = plan == nil ? .unavailable : .loaded
        case .cancelled:
            if phase == .loading { phase = plan == nil ? .idle : .loaded }
        }
    }

    private func notifyPlanChanged() {
        onPlanChanged?(plan)
    }

    /// A fresh read of one project (e.g. `GET /projects/{id}` from the directory) is newer than
    /// this cache: drop the cached copy and any list still in flight, so `projectName` falls back
    /// to the directory until the next load.
    func invalidateProject(_ id: UUID) {
        projects[id] = nil
        publishedTickets[.projects] = issueTicket()
    }

    // MARK: editing (timetable.md §5)

    /// Drag/resize release or Edit time: `edit` → staged pin (202) → review opens at once.
    func edit(_ block: Block, to span: Span) async {
        guard DragGuard.canPickUp(classification(for: block)) else {
            notice = .weekChanged
            return
        }
        switch DragGuard.decide(original: block.span, proposed: span, now: now()) {
        case .unchanged:
            return
        case .rejectedPast:
            rejectPastDrop()
            return
        case .send(let span):
            await sendStaged(.edit(span: span), for: block, placeholder: .moved(to: span))
        }
    }

    /// Context menu › Delete or the trash target: `delete` → staged forbid (202).
    func delete(_ block: Block) async {
        guard DragGuard.canPickUp(classification(for: block)) else {
            notice = .weekChanged
            return
        }
        await sendStaged(.delete, for: block, placeholder: .deleted(block.span))
    }

    /// "Mark done" on a started block: applied directly (200 + Block, D5).
    func markDone(_ block: Block) async {
        await sendApplied(.complete, for: block)
    }

    /// "I skipped this" on a started block (200 + Block, D5).
    func skip(_ block: Block) async {
        await sendApplied(.skip, for: block)
    }

    /// "Skip this" from a block-start notification. The lineage finds the block in the current
    /// plan (the notification may predate a re-plan); the notified block id is the fallback.
    func skip(lineageId: UUID, fallbackBlockId: UUID?) async {
        if plan == nil { await load() }
        if let block = plan?.blocks.first(where: { $0.lineageId == lineageId }) {
            await skip(block)
            return
        }
        guard let fallbackBlockId else { return }
        if case .applied(let updated)? = try? await api.patchBlock(id: fallbackBlockId, action: .skip) {
            replace(updated)
        }
    }

    func rejectPastDrop() {
        rejectedDropCount += 1
        notice = .pastDrop
    }

    func show(_ notice: Notice) {
        self.notice = notice
    }

    func dismissNotice() {
        notice = nil
    }

    /// "Try it" on a relaxation: the matching staged write; its 202 replaces the proposal under
    /// review. Never applied automatically.
    func tryRelaxation(_ relaxation: Relaxation) async -> RelaxationOutcome {
        let started = generation
        do {
            let changeset = try await RelaxationRequest(relaxation).send(api)
            guard started == generation else { return .ignored }
            directory.register(changeset)
            return pending.stage(changeset) ? .staged : .ignored
        } catch {
            guard started == generation else { return .ignored }
            if let apiError = error as? APIError, case .server(_, let message, _, _) = apiError, apiError.serverCode == KnownErrorCode.validationFailed {
                return .failed(message)
            }
            return .failed("Couldn't do that. Nothing changed; try again.")
        }
    }

    /// Wired to `PendingChangesetStore.onConfirmed`: what changed pulses once on the new plan.
    func noteConfirmed(_ changeset: Changeset) {
        // The confirmed changeset is newer than any snapshot in flight: drop the project cache
        // (renames are folded into the directory) and any plan/projects load issued before it.
        projects = [:]
        publishedTickets[.projects] = issueTicket()
        publishedTickets[.plan] = issueTicket()
        pulseLineages = Set((changeset.diff?.entries ?? []).compactMap { entry in
            switch entry.change {
            case .moved, .added: entry.lineageId
            case .removed, .unchanged: nil
            }
        })
    }

    func clearPulse() {
        pulseLineages = []
    }

    /// Sign-out: forget the previous account's week.
    func reset() {
        generation += 1
        phase = .idle
        plan = nil
        pins = []
        projects = [:]
        preferences = nil
        exceptions = []
        connectivity = .online
        notice = nil
        inFlight = [:]
        pulseLineages = []
        requestedDay = nil
        publishedTickets = [:]
        notifyPlanChanged()
    }

    // MARK: private

    private func sendStaged(_ action: BlockAction, for block: Block, placeholder: StagedBlockEdit) async {
        let started = generation
        inFlight[block.id] = placeholder
        defer { if started == generation { inFlight[block.id] = nil } }
        do {
            let result = try await api.patchBlock(id: block.id, action: action)
            guard started == generation else { return }
            switch result {
            case .staged(let changeset):
                directory.register(changeset)
                if pending.stage(changeset) { navigation.presentReview() }
            case .applied(let updated):
                replace(updated)
            }
        } catch {
            guard started == generation else { return }
            await handleWriteError(error)
        }
    }

    private func sendApplied(_ action: BlockAction, for block: Block) async {
        let started = generation
        do {
            let result = try await api.patchBlock(id: block.id, action: action)
            guard started == generation else { return }
            switch result {
            case .applied(let updated):
                replace(updated)
            case .staged(let changeset):
                if pending.stage(changeset) { navigation.presentReview() }
            }
        } catch {
            guard started == generation else { return }
            await handleWriteError(error)
        }
    }

    /// Swaps in the server's copy of a block (complete/skip). Not a placement change.
    private func replace(_ updated: Block) {
        guard let plan, plan.blocks.contains(where: { $0.id == updated.id }) else { return }
        self.plan = WeekPlan(
            id: plan.id, horizon: plan.horizon, weekStart: plan.weekStart, timezone: plan.timezone,
            version: plan.version, status: plan.status, infeasibility: plan.infeasibility,
            createdAt: plan.createdAt, supersededAt: plan.supersededAt,
            blocks: plan.blocks.map { $0.id == updated.id ? updated : $0 }
        )
        publishedTickets[.plan] = issueTicket()
        notifyPlanChanged()
    }

    private func handleWriteError(_ error: any Error) async {
        guard let apiError = error as? APIError else {
            if !(error is CancellationError) { notice = .failed }
            return
        }
        switch apiError.serverCode {
        case KnownErrorCode.blockSuperseded, KnownErrorCode.blockImmutable:
            // The block isn't in the live plan any more (or has started): refetch, then retry.
            notice = .weekChanged
            await load()
        case KnownErrorCode.blockNotStarted:
            notice = .notStarted
            await load()
        default:
            if case .transport = apiError {
                notice = .unreachable
            } else {
                notice = .failed
            }
        }
    }
}
