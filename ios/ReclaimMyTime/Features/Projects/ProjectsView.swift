import SwiftUI

/// The Projects tab: the server's project list, one row per project. Archived projects appear only
/// when "Show archived" is on. Read-only: changes go through Chat.
struct ProjectsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let list = model.projectList
        NavigationStack {
            content(list)
                .navigationTitle("Projects")
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Menu {
                            Toggle("Show archived", isOn: Binding(
                                get: { list.showsArchived },
                                set: { value in Task { await list.setShowsArchived(value) } }
                            ))
                        } label: {
                            Label("Filter", systemImage: list.showsArchived
                                  ? "line.3.horizontal.decrease.circle.fill"
                                  : "line.3.horizontal.decrease.circle")
                        }
                    }
                }
                .task { await list.load() }
        }
    }

    @ViewBuilder
    private func content(_ list: ProjectsModel) -> some View {
        switch list.phase {
        case .idle, .loading:
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let failure):
            ContentUnavailableView {
                Label(failure.title, systemImage: failure == .offline ? "wifi.slash" : "exclamationmark.triangle")
            } description: {
                Text("Nothing changed. Try again in a moment.")
            } actions: {
                Button("Retry") { Task { await list.load() } }
                    .buttonStyle(.borderedProminent)
            }
        case .loaded:
            if list.projects.isEmpty {
                ScrollView {
                    emptyState(showsArchived: list.showsArchived)
                        .containerRelativeFrame(.vertical)
                }
                .refreshable { await list.load() }
            } else {
                List {
                    if let failure = list.refreshFailure {
                        Section {
                            Label("\(failure.title) Showing your last list.", systemImage: "exclamationmark.triangle")
                                .font(.rmt.callout)
                                .foregroundStyle(ColorToken.warning)
                        }
                    }
                    ForEach(list.projects) { project in
                        // Archived projects get no swatch: `slot(for:)` assigns and persists a
                        // palette slot on first sight, which would crowd out active projects.
                        ProjectRow(project: project, slot: project.status == .archived ? nil : model.palette.slot(for: project.id))
                    }
                }
                .refreshable { await list.load() }
            }
        }
    }

    private func emptyState(showsArchived: Bool) -> some View {
        ContentUnavailableView {
            Label("No projects yet", systemImage: "folder")
        } description: {
            Text(showsArchived
                 ? "Nothing here, archived or otherwise. Tell the assistant in Chat what you're working on and it will set up your projects."
                 : "Tell the assistant in Chat what you're working on and it will set up your projects. They'll show up here once you confirm them.")
        }
    }
}

/// One project: name, kind, the key figure, and a status tag when it isn't active. Archived rows
/// are muted.
struct ProjectRow: View {
    let project: Project
    /// Nil for archived projects (drawn as a neutral outline).
    let slot: PaletteSlot?
    var text = ProjectRowText()

    var body: some View {
        let muted = text.isMuted(project)
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Circle()
                .fill(slot?.fill ?? .clear)
                .overlay(Circle().strokeBorder(slot?.edge ?? ColorToken.textSecondary, lineWidth: 1))
                .frame(width: 12, height: 12)
                .opacity(muted ? 0.5 : 1)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Text(text.title(project))
                        .font(.rmt.headline)
                        .foregroundStyle(muted ? ColorToken.textSecondary : ColorToken.textPrimary)
                    Spacer(minLength: Spacing.s)
                    if let tag = text.statusTag(project) {
                        Text(tag)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(ColorToken.textSecondary)
                            .padding(.horizontal, Spacing.s)
                            .padding(.vertical, 2)
                            .background(ColorToken.surfaceRaised, in: Capsule())
                    }
                }
                Text(text.kindLabel(project))
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
                if let figure = text.figure(project) {
                    Text(figure)
                        .font(.rmt.body)
                        .monospacedDigit()
                        .foregroundStyle(muted ? ColorToken.textSecondary : ColorToken.textPrimary)
                }
            }
        }
        .padding(.vertical, Spacing.xs)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text.accessibilityLabel(project))
    }
}
