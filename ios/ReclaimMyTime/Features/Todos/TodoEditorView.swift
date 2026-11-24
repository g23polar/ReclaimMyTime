import SwiftUI

/// Create or edit a todo. Kind is chosen at create and read-only after. An edit sends only the
/// fields that changed, so an unchanged link to a since-archived project is never re-sent (the
/// server rejects linking an archived project with 409 `project_unavailable`).
struct TodoEditorView: View {
    @Environment(\.dismiss) private var dismiss
    let todos: TodosModel
    /// Nil when creating.
    let editing: Todo?

    @State private var kind: TodoKind
    @State private var title: String
    @State private var notes: String
    @State private var hasDueDate: Bool
    @State private var dueDate: Date
    @State private var projectId: UUID?
    @State private var saving = false
    @State private var errorMessage: String?

    init(todos: TodosModel, editing: Todo?) {
        self.todos = todos
        self.editing = editing
        let zone = TimeZone.current
        // A new one-off is prefilled with today. The server does the same when the date is left
        // out, so clearing the field on create means "today".
        let initialDue: LocalDate? = editing == nil ? LocalDate.from(Date(), timeZone: zone) : editing?.dueDate
        _kind = State(initialValue: editing?.kind ?? .oneOff)
        _title = State(initialValue: editing?.title ?? "")
        _notes = State(initialValue: editing?.notes ?? "")
        _hasDueDate = State(initialValue: initialDue != nil)
        _dueDate = State(initialValue: initialDue?.middayInstant(timeZone: zone) ?? Date())
        _projectId = State(initialValue: editing?.projectId)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Title", text: $title)
                    if editing == nil {
                        Picker("Kind", selection: $kind) {
                            ForEach([TodoKind.oneOff, .daily, .weekly], id: \.self) { option in
                                Text(option.label).tag(option)
                            }
                        }
                    } else {
                        LabeledContent("Kind", value: kind.label)
                    }
                }

                Section("Notes") {
                    TextField("Notes", text: $notes, axis: .vertical)
                        .lineLimit(3...8)
                }

                if kind == .oneOff {
                    Section {
                        if hasDueDate {
                            DatePicker("Due", selection: $dueDate, displayedComponents: .date)
                            Button("Clear due date") { hasDueDate = false }
                        } else {
                            Button("Add due date") { hasDueDate = true }
                        }
                    } footer: {
                        if !hasDueDate {
                            Text(editing == nil ? "No date means today." : "No due date.")
                        }
                    }
                }

                Section {
                    Picker("Project", selection: $projectId) {
                        Text("None").tag(UUID?.none)
                        ForEach(projectOptions) { project in
                            Text(project.name).tag(Optional(project.id))
                        }
                    }
                }

                if let errorMessage {
                    Section {
                        Label(errorMessage, systemImage: "exclamationmark.triangle")
                            .font(.rmt.callout)
                            .foregroundStyle(ColorToken.warning)
                    }
                }
            }
            .navigationTitle(editing == nil ? "New todo" : "Edit todo")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: save)
                        .disabled(!canSave)
                }
            }
        }
    }

    // MARK: validation and payloads

    private var trimmedTitle: String {
        title.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var trimmedNotes: String? {
        let value = notes.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }

    private var dueLocalDate: LocalDate? {
        hasDueDate ? LocalDate.from(dueDate, timeZone: .current) : nil
    }

    /// The contract's bounds: title 1–200 characters, notes at most 2000.
    private var canSave: Bool {
        !saving
            && (1...200).contains(trimmedTitle.unicodeScalars.count)
            && (trimmedNotes?.unicodeScalars.count ?? 0) <= 2000
    }

    /// Active, paused and done projects, plus the todo's current project even when archived, so the
    /// row's own link still shows in the picker.
    private var projectOptions: [Project] {
        var options = todos.projects.filter { [ProjectStatus.active, .paused, .done].contains($0.status) }
        if let current = editing?.projectId,
           !options.contains(where: { $0.id == current }),
           let project = todos.projects.first(where: { $0.id == current }) {
            options.append(project)
        }
        return options
    }

    private func makeDraft() -> TodoDraft {
        TodoDraft(
            kind: kind,
            title: trimmedTitle,
            notes: trimmedNotes,
            dueDate: kind == .oneOff ? dueLocalDate : nil,
            projectId: projectId
        )
    }

    private func makePatch(for todo: Todo) -> TodoPatch? {
        TodosModel.patch(
            for: todo,
            title: trimmedTitle,
            notes: trimmedNotes,
            dueDate: dueLocalDate,
            projectId: projectId
        )
    }

    private func save() {
        saving = true
        errorMessage = nil
        Task {
            defer { saving = false }
            do {
                if let editing {
                    if let patch = makePatch(for: editing) {
                        try await todos.update(editing, patch: patch)
                    }
                } else {
                    try await todos.create(makeDraft())
                }
                dismiss()
            } catch {
                errorMessage = TodosModel.editorMessage(for: error)
            }
        }
    }
}
