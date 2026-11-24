import Foundation
import Observation

/// The Todos tab. `GET /todos` plus the seven todo writes. Ticks and one-off completion are
/// optimistic: the row changes at once, and a failure rolls it back with an inline caption on that
/// row. Every server row replaces the local one on success. The client never computes periods: a
/// recurring tick sends the `periods` date from the last list response.
@MainActor
@Observable
final class TodosModel {
    enum Phase: Hashable, Sendable {
        case idle
        /// First fetch, nothing to show yet.
        case loading
        case loaded
        /// The first fetch failed; there is no list to show.
        case failed(Failure)
    }

    enum Failure: Hashable, Sendable {
        /// Transport failure: the request never got an answer.
        case offline
        /// The server answered with an error, or a body that didn't match the contract.
        case server

        var title: String {
            switch self {
            case .offline: "You're offline."
            case .server: "Couldn't load your todos."
            }
        }
    }

    /// The inline caption on a row whose optimistic change was rolled back.
    static let saveFailedCaption = "Couldn't save. Try again."

    private(set) var phase: Phase = .idle
    /// Every todo the server returned, in no particular order; the sections are derived below.
    private(set) var todos: [Todo] = []
    /// The current period starts from the last list response. Nil before the first load.
    private(set) var periods: TodoPeriods?
    /// Projects for the row labels and the picker, archived included (archived names are shown
    /// on rows that link to them). Best effort: a failed fetch keeps the previous names.
    private(set) var projects: [Project] = []
    /// A refresh failed while an earlier list stays visible.
    private(set) var refreshFailure: Failure?
    /// Inline captions for rolled-back ticks, keyed by todo id. Cleared on the next attempt.
    private(set) var rowErrors: [UUID: String] = [:]
    /// List-level message for a failed delete or reorder.
    private(set) var actionError: String?

    private let api: any APIClient
    private let now: @Sendable () -> Date
    private let timeZone: @Sendable () -> TimeZone
    /// Bumped by every load and by `reset()`; a slow answer for an older load is dropped.
    private var ticket = 0
    /// Todos with a write in flight. A second tap on the same row is ignored until it settles.
    private var inFlight: Set<UUID> = []
    /// Kinds with a reorder in flight. Overlapping reorders would send stale id sets.
    private var reorderInFlight: Set<TodoKind> = []

    init(
        api: any APIClient,
        now: @escaping @Sendable () -> Date = { Date() },
        timeZone: @escaping @Sendable () -> TimeZone = { .current }
    ) {
        self.api = api
        self.now = now
        self.timeZone = timeZone
    }

    // MARK: sections (derived, display only)

    /// Daily todos, manual order.
    var today: [Todo] { ofKind(.daily) }
    /// Weekly todos, manual order.
    var thisWeek: [Todo] { ofKind(.weekly) }
    /// Open one-offs, manual order.
    var openOneOffs: [Todo] { ofKind(.oneOff).filter { $0.completedAt == nil } }
    /// Completed one-offs, most recently completed first. The server decides which ones are listed.
    var doneOneOffs: [Todo] {
        ofKind(.oneOff)
            .filter { $0.completedAt != nil }
            .sorted { ($0.completedAt ?? .distantPast) > ($1.completedAt ?? .distantPast) }
    }

    /// The current period start for a recurring kind, from the last list response.
    func currentPeriod(for kind: TodoKind) -> LocalDate? {
        switch kind {
        case .daily: periods?.daily
        case .weekly: periods?.weekly
        case .oneOff: nil
        }
    }

    /// Whether a recurring todo is ticked for the current period.
    func isTicked(_ todo: Todo) -> Bool {
        guard let period = currentPeriod(for: todo.kind) else { return false }
        return todo.completedPeriods.contains(period)
    }

    /// A one-off whose due date is before today on the device's calendar. Display only.
    func isOverdue(_ todo: Todo) -> Bool {
        guard !todo.kind.isRecurring, todo.completedAt == nil, let due = todo.dueDate,
              let today = LocalDate.from(now(), timeZone: timeZone()) else { return false }
        return due < today
    }

    func projectName(for todo: Todo) -> String? {
        guard let id = todo.projectId else { return nil }
        return projects.first { $0.id == id }?.name
    }

    // MARK: load

    func load() async {
        ticket += 1
        let current = ticket
        if phase != .loaded { phase = .loading }
        do {
            async let listed = api.listTodos(includeDone: true)
            async let named = try? api.listProjects(includeArchived: true)
            let list = try await listed
            let names = await named
            guard current == ticket else { return }
            todos = list.todos
            periods = list.periods
            if let names { projects = names }
            refreshFailure = nil
            phase = .loaded
        } catch {
            guard current == ticket else { return }
            let failure = Self.failure(for: error)
            if phase == .loaded {
                refreshFailure = failure
            } else {
                phase = .failed(failure)
            }
        }
    }

    /// Clears per-user state on sign-out.
    func reset() {
        ticket += 1
        phase = .idle
        todos = []
        periods = nil
        projects = []
        refreshFailure = nil
        rowErrors = [:]
        actionError = nil
        inFlight = []
        reorderInFlight = []
    }

    // MARK: ticks (optimistic)

    /// Ticks or unticks a recurring todo for its current period (`PUT` / `DELETE` completion).
    func toggleRecurring(_ todo: Todo) async {
        guard todo.kind.isRecurring, let period = currentPeriod(for: todo.kind), begin(todo.id) else { return }
        defer { end(todo.id) }
        let before = todo
        let ticking = !isTicked(todo)
        let periodsAfter = ticking
            ? todo.completedPeriods + [period]
            : todo.completedPeriods.filter { $0 != period }
        replaceRow(Todo(todo, completedAt: nil, completedPeriods: periodsAfter))
        do {
            let updated = ticking
                ? try await api.completeTodoPeriod(id: todo.id, periodStart: period)
                : try await api.uncompleteTodoPeriod(id: todo.id, periodStart: period)
            replaceRow(updated)
        } catch {
            restoreRow(before)
        }
    }

    /// Completes or reopens a one-off (`PATCH {done}`).
    func toggleOneOff(_ todo: Todo) async {
        guard !todo.kind.isRecurring, begin(todo.id) else { return }
        defer { end(todo.id) }
        let before = todo
        let completing = todo.completedAt == nil
        replaceRow(Todo(todo, completedAt: completing ? now() : nil, completedPeriods: []))
        do {
            replaceRow(try await api.updateTodo(id: todo.id, patch: TodoPatch(done: completing)))
        } catch {
            restoreRow(before)
        }
    }

    // MARK: writes

    /// Creates a todo from the editor. Throws so the editor can show the server's message.
    func create(_ draft: TodoDraft) async throws {
        todos.append(try await api.createTodo(draft))
    }

    /// Saves an edit from the editor. Throws so the editor can show the server's message.
    func update(_ todo: Todo, patch: TodoPatch) async throws {
        replaceRow(try await api.updateTodo(id: todo.id, patch: patch))
    }

    /// Hard delete (cascades to completions). Removed at once; restored on failure.
    func delete(_ todo: Todo) async {
        guard begin(todo.id) else { return }
        defer { end(todo.id) }
        let snapshot = todos
        todos.removeAll { $0.id == todo.id }
        do {
            try await api.deleteTodo(id: todo.id)
            actionError = nil
        } catch {
            todos = snapshot
            actionError = "Couldn't delete that todo. Try again."
        }
    }

    /// Applies a `List.onMove` inside one section. The request carries the kind's whole id set:
    /// the open one-offs are reordered among their own slots, and completed one-offs keep theirs.
    func move(kind: TodoKind, fromOffsets source: IndexSet, toOffset destination: Int) async {
        guard !reorderInFlight.contains(kind) else { return }
        let all = ofKind(kind)
        let visible = kind == .oneOff ? openOneOffs : all
        let visibleIds = Set(visible.map(\.id))
        var reordered = Self.moved(visible, fromOffsets: source, toOffset: destination).makeIterator()
        let ids = all.map { todo in visibleIds.contains(todo.id) ? reordered.next()!.id : todo.id }
        guard ids != all.map(\.id) else { return }

        reorderInFlight.insert(kind)
        defer { reorderInFlight.remove(kind) }
        let snapshot = todos
        applyOrder(ids)
        do {
            let list = try await api.reorderTodos(TodoOrder(kind: kind, ids: ids))
            for todo in list.todos { replaceRow(todo) }
            periods = list.periods
            actionError = nil
        } catch {
            todos = snapshot
            actionError = "Couldn't reorder those todos. Try again."
        }
    }

    /// The fields an edit changed, as a patch; nil when nothing changed (no request is sent).
    /// Due date is never patched on a recurring todo.
    static func patch(
        for todo: Todo,
        title: String,
        notes: String?,
        dueDate: LocalDate?,
        projectId: UUID?
    ) -> TodoPatch? {
        var patch = TodoPatch()
        if title != todo.title {
            patch.title = title
        }
        if notes != todo.notes {
            patch.notes = notes.map { .value($0) } ?? .null
        }
        if todo.kind == .oneOff, dueDate != todo.dueDate {
            patch.dueDate = dueDate.map { .value($0) } ?? .null
        }
        if projectId != todo.projectId {
            patch.projectId = projectId.map { .value($0) } ?? .null
        }
        let changed = patch.title != nil || patch.notes != nil || patch.dueDate != nil || patch.projectId != nil
        return changed ? patch : nil
    }

    // MARK: helpers

    /// `List.onMove`'s rule, without SwiftUI: items at `source` move to just before `destination`
    /// (an index in the original array).
    static func moved<Item>(_ items: [Item], fromOffsets source: IndexSet, toOffset destination: Int) -> [Item] {
        let moving = source.map { items[$0] }
        var rest = items.enumerated().filter { !source.contains($0.offset) }.map(\.element)
        let insertAt = destination - source.filter { $0 < destination }.count
        rest.insert(contentsOf: moving, at: insertAt)
        return rest
    }

    static func failure(for error: any Error) -> Failure {
        switch error {
        case APIError.transport, is URLError: .offline
        default: .server
        }
    }

    /// The message the editor shows for a failed save: the server's own wording when it sent any.
    static func editorMessage(for error: any Error) -> String {
        if case APIError.transport = error { return "You're offline. Nothing was saved." }
        if error is URLError { return "You're offline. Nothing was saved." }
        if let body = (error as? APIError)?.errorBody { return body.message }
        return "Couldn't save. Try again."
    }

    private func ofKind(_ kind: TodoKind) -> [Todo] {
        todos.filter { $0.kind == kind }.sorted { $0.position < $1.position }
    }

    /// Sets each id's position to its index within its kind, so the sections re-sort at once.
    private func applyOrder(_ ids: [UUID]) {
        let rank = Dictionary(uniqueKeysWithValues: ids.enumerated().map { ($1, $0) })
        todos = todos.map { todo in
            guard let index = rank[todo.id] else { return todo }
            return Todo(todo, position: index)
        }
    }

    private func replaceRow(_ todo: Todo) {
        if let index = todos.firstIndex(where: { $0.id == todo.id }) {
            todos[index] = todo
        }
    }

    /// Rolls one row back to its pre-tick state and shows the caption on it. A row that a reload
    /// has removed is left out.
    private func restoreRow(_ todo: Todo) {
        guard todos.contains(where: { $0.id == todo.id }) else { return }
        replaceRow(todo)
        rowErrors[todo.id] = Self.saveFailedCaption
    }

    /// Marks a write as started. Returns false when a write for this todo is already in flight.
    private func begin(_ id: UUID) -> Bool {
        guard inFlight.insert(id).inserted else { return false }
        rowErrors[id] = nil
        return true
    }

    private func end(_ id: UUID) {
        inFlight.remove(id)
    }
}

extension Todo {
    /// A copy with optimistic completion state. Used only until the server's row replaces it.
    init(_ base: Todo, completedAt: Date?, completedPeriods: [LocalDate]) {
        self.init(base, completedAt: completedAt, completedPeriods: completedPeriods, position: base.position)
    }

    /// A copy with a new manual position (optimistic reorder).
    init(_ base: Todo, position: Int) {
        self.init(base, completedAt: base.completedAt, completedPeriods: base.completedPeriods, position: position)
    }

    private init(_ base: Todo, completedAt: Date?, completedPeriods: [LocalDate], position: Int) {
        id = base.id
        kind = base.kind
        title = base.title
        notes = base.notes
        dueDate = base.dueDate
        projectId = base.projectId
        projectArchived = base.projectArchived
        self.position = position
        self.completedAt = completedAt
        self.completedPeriods = completedPeriods
        createdAt = base.createdAt
    }
}
