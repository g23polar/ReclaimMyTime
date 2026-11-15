import SwiftUI

/// Edit sheet for a staged project (onboarding.md §4): a standard Form prefilled from the card.
struct ProjectEditSheet: View {
    @State var model: ProjectEditModel
    let onSaved: (Changeset) async -> Void
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Project") {
                    TextField("Name", text: $model.name)
                }
                switch model.kind {
                case .deadline:
                    Section("Deadline") {
                        DatePicker("Due", selection: $model.targetDate, displayedComponents: .date)
                        Stepper(value: $model.estimatedTotalHours, in: 0.5...999, step: 0.5) {
                            LabeledContent("Total", value: "\(ProjectCardText().formatHours(model.estimatedTotalHours)) h")
                        }
                    }
                case .perennial:
                    Section("Every week") {
                        Stepper(value: $model.weeklyTargetHours, in: 0.5...168, step: 0.5) {
                            LabeledContent("Hours a week", value: "\(ProjectCardText().formatHours(model.weeklyTargetHours)) h")
                        }
                    }
                case nil:
                    EmptyView()
                }
                Section("Sittings") {
                    Stepper(value: $model.maxBlockMinutes, in: 15...720, step: 15) {
                        LabeledContent("Up to", value: ProjectCardText().formatDuration(minutes: model.maxBlockMinutes))
                    }
                    if model.original.minBlockMinutes == nil {
                        Toggle("Set a minimum", isOn: $model.hasMinimum)
                    }
                    if model.hasMinimum {
                        Stepper(value: $model.minBlockMinutes, in: 5...model.maxBlockMinutes, step: 5) {
                            LabeledContent("At least", value: ProjectCardText().formatDuration(minutes: model.minBlockMinutes))
                        }
                    }
                }
                Section {
                    ForEach(model.requirements, id: \.self) { tag in
                        Text(tag)
                    }
                    .onDelete { offsets in
                        for tag in offsets.map({ model.requirements[$0] }) { model.removeTag(tag) }
                    }
                    HStack {
                        TextField("Add a requirement", text: $model.newTag)
                            .textInputAutocapitalization(.never)
                            .onSubmit { model.addTag() }
                        Button("Add") { model.addTag() }
                            .disabled(model.newTag.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                } header: {
                    Text("Needs")
                } footer: {
                    Text("Things this project needs, like a laptop or the gym.")
                }
                if let error = model.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(ColorToken.warning)
                    }
                }
            }
            .navigationTitle("Edit project")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if model.isSaving {
                        ProgressView()
                    } else {
                        Button("Save") {
                            Task {
                                guard model.makePatch() != nil else { return dismiss() }
                                if let changeset = await model.save(api: app.api) {
                                    await onSaved(changeset)
                                    dismiss()
                                }
                            }
                        }
                        .disabled(!model.canSave)
                    }
                }
            }
        }
    }
}
