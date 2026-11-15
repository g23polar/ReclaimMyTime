import Foundation
import Observation

/// What this client knows about projects, for rendering cards from bare `project_updated`
/// mutations. Seeded from staged creations and confirmed changesets; unknown ids are fetched with
/// `GET /projects/{id}` (staged projects 404 by D23, which is fine: they are seeded from staging).
@MainActor
@Observable
final class ProjectDirectory {
    private(set) var known: [UUID: ProjectCard] = [:]
    /// Names applied directly by a name-only `PATCH /projects` (200), newer than any staged draft.
    private var appliedNames: [UUID: String] = [:]
    private var inFlight: Set<UUID> = []
    private var generation = 0
    private let api: any APIClient
    /// Called after a successful `GET /projects/{id}`: other caches of that project are older.
    var onProjectRead: (@MainActor (Project) -> Void)?

    init(api: any APIClient) {
        self.api = api
    }

    func card(for id: UUID) -> ProjectCard? {
        known[id]
    }

    /// Seeds staged creations so their later updates render with a name.
    func register(_ changeset: Changeset) {
        for mutation in changeset.staged {
            if case let .projectCreated(projectId, draft) = mutation.change {
                known[projectId] = ProjectCard(id: projectId, draft: draft, isNew: true)
            }
        }
    }

    func appliedName(for id: UUID) -> String? {
        appliedNames[id]
    }

    /// A name-only edit came back applied (200 + Project): the server's row is the newest copy.
    func applyApplied(_ project: Project) {
        var card = ProjectCard(project: project)
        card.isNew = known[project.id]?.isNew ?? false
        known[project.id] = card
        appliedNames[project.id] = project.name
        onProjectRead?(project)
    }

    /// After confirm the staged values are live: fold them in.
    func applyConfirmed(_ changeset: Changeset) {
        for card in ProjectCardDerivation.cards(from: changeset, base: { known[$0] }) {
            known[card.id] = card
        }
    }

    /// Fetches bases for `project_updated` targets this client hasn't seen. Failures are ignored:
    /// the card then shows only the staged fields.
    func ensureBases(for changeset: Changeset) async {
        let created = Set(changeset.staged.compactMap { mutation -> UUID? in
            if case let .projectCreated(id, _) = mutation.change { return id }
            return nil
        })
        let missing = changeset.staged.compactMap { mutation -> UUID? in
            if case let .projectUpdated(id, _) = mutation.change, !created.contains(id), known[id] == nil, !inFlight.contains(id) {
                return id
            }
            return nil
        }
        let started = generation
        for id in Set(missing) {
            inFlight.insert(id)
            let project = try? await api.getProject(id: id)
            guard started == generation else { return }
            inFlight.remove(id)
            if let project {
                known[id] = ProjectCard(project: project)
                onProjectRead?(project)
            }
        }
    }

    func reset() {
        generation += 1
        inFlight.removeAll()
        known.removeAll()
        appliedNames.removeAll()
    }
}
