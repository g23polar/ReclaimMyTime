import SwiftUI

/// The Todos tab: daily and weekly checklists, then open one-offs, then a collapsed Done section.
/// Standalone tracking: nothing here touches the solver or the calendar.
struct TodosView: View {
    @Environment(AppModel.self) private var model
    @State private var creating = false
    @State private var editing: Todo?
    @State private var doneExpanded = false

    var body: some View {
        let todos = model.todos
        NavigationStack {
            content(todos)
                .navigationTitle("Todos")
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        EditButton()
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { creating = true } label: {
                            Label("New todo", systemImage: "plus")
                        }
                    }
                }
                .sheet(isPresented: $creating) {
                    TodoEditorView(todos: todos, editing: nil)
                }
                .sheet(item: $editing) { todo in
                    TodoEditorView(todos: todos, editing: todo)
                }
                .task { await todos.load() }
        }
    }

    @ViewBuilder
    private func content(_ todos: TodosModel) -> some View {
        switch todos.phase {
        case .idle, .loading:
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let failure):
            ContentUnavailableView {
                Label(failure.title, systemImage: failure == .offline ? "wifi.slash" : "exclamationmark.triangle")
            } description: {
                Text("Nothing changed. Try again in a moment.")
            } actions: {
                Button("Retry") { Task { await todos.load() } }
                    .buttonStyle(.borderedProminent)
            }
        case .loaded:
            List {
                if let failure = todos.refreshFailure {
                    Section {
                        Label("\(failure.title) Showing your last list.", systemImage: "exclamationmark.triangle")
                            .font(.rmt.callout)
                            .foregroundStyle(ColorToken.warning)
                    }
                }
                if let message = todos.actionError {
                    Section {
                        Label(message, systemImage: "exclamationmark.triangle")
                            .font(.rmt.callout)
                            .foregroundStyle(ColorToken.warning)
                    }
                }
                recurringSection(todos, title: "Today", kind: .daily, rows: todos.today, empty: "No daily todos yet.")
                recurringSection(todos, title: "This week", kind: .weekly, rows: todos.thisWeek, empty: "No weekly todos yet.")
                oneOffSection(todos)
                doneSection(todos)
            }
            .refreshable { await todos.load() }
        }
    }

    private func recurringSection(_ todos: TodosModel, title: String, kind: TodoKind, rows: [Todo], empty: String) -> some View {
        Section {
            if rows.isEmpty {
                Text(empty)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
            }
            ForEach(rows) { todo in
                row(todo, todos)
            }
            .onMove { source, destination in
                Task { await todos.move(kind: kind, fromOffsets: source, toOffset: destination) }
            }
        } header: {
            Text(title)
        }
    }

    private func oneOffSection(_ todos: TodosModel) -> some View {
        let rows = todos.openOneOffs
        return Section {
            if rows.isEmpty {
                Text("No open one-off todos.")
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
            }
            ForEach(rows) { todo in
                row(todo, todos)
            }
            .onMove { source, destination in
                Task { await todos.move(kind: .oneOff, fromOffsets: source, toOffset: destination) }
            }
        } header: {
            Text("One-off")
        }
    }

    private func doneSection(_ todos: TodosModel) -> some View {
        let rows = todos.doneOneOffs
        return Section {
            DisclosureGroup(isExpanded: $doneExpanded) {
                if rows.isEmpty {
                    Text("Nothing completed recently.")
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.textSecondary)
                }
                ForEach(rows) { todo in
                    row(todo, todos)
                }
            } label: {
                Text("Done (\(rows.count))")
                    .font(.rmt.headline)
            }
        }
    }

    private func row(_ todo: Todo, _ todos: TodosModel) -> some View {
        TodoRow(
            todo: todo,
            isDone: todo.kind.isRecurring ? todos.isTicked(todo) : todo.completedAt != nil,
            isOverdue: todos.isOverdue(todo),
            projectName: todos.projectName(for: todo),
            caption: todos.rowErrors[todo.id],
            onToggle: {
                Task {
                    if todo.kind.isRecurring {
                        await todos.toggleRecurring(todo)
                    } else {
                        await todos.toggleOneOff(todo)
                    }
                }
            },
            onOpen: { editing = todo }
        )
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            Button(role: .destructive) {
                Task { await todos.delete(todo) }
            } label: {
                Label("Delete", systemImage: "trash")
            }
        }
    }
}

/// One todo row. The check toggles completion (a tick, or done/undo for one-offs); the text opens
/// the editor. Display only: the parent decides every value.
struct TodoRow: View {
    let todo: Todo
    let isDone: Bool
    let isOverdue: Bool
    let projectName: String?
    let caption: String?
    let onToggle: () -> Void
    let onOpen: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Button(action: onToggle) {
                Image(systemName: isDone ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundStyle(isDone ? ColorToken.positive : ColorToken.textSecondary)
            }
            .buttonStyle(.borderless)
            .accessibilityLabel(isDone ? "Mark \(todo.title) not done" : "Mark \(todo.title) done")

            Button(action: onOpen) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(todo.title)
                        .font(.rmt.body)
                        .foregroundStyle(isDone ? ColorToken.textSecondary : ColorToken.textPrimary)
                        .strikethrough(isDone)
                    if let notes = todo.notes {
                        Text(notes)
                            .font(.rmt.callout)
                            .foregroundStyle(ColorToken.textSecondary)
                            .lineLimit(1)
                    }
                    meta
                    if let caption {
                        Text(caption)
                            .font(.rmt.callout)
                            .foregroundStyle(ColorToken.warning)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
    }

    private var meta: some View {
        HStack(spacing: 8) {
            if !todo.kind.isRecurring, let due = todo.dueDate {
                Text("Due \(Self.dateLabel(due))")
                    .foregroundStyle(isOverdue ? ColorToken.warning : ColorToken.textSecondary)
            }
            if let projectName {
                Text(projectName)
                    .foregroundStyle(ColorToken.textSecondary)
                    .lineLimit(1)
            }
            if todo.projectArchived {
                Text("Archived")
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(ColorToken.surfaceRaised, in: Capsule())
                    .foregroundStyle(ColorToken.textSecondary)
            }
        }
        .font(.rmt.blockMeta)
    }

    /// The date as the device shows it. Display formatting only; no date arithmetic.
    static func dateLabel(_ date: LocalDate) -> String {
        date.middayInstant(timeZone: .current)?.formatted(date: .abbreviated, time: .omitted) ?? date.description
    }
}

extension TodoKind {
    var label: String {
        switch self {
        case .daily: "Daily"
        case .weekly: "Weekly"
        case .oneOff: "One-off"
        }
    }
}
