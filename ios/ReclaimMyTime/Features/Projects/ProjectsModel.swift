import Foundation
import Observation

/// The Projects tab: `GET /projects?include_archived=`. Shows the server's list in the server's
/// order (created_at ascending); nothing is filtered, sorted or judged client-side.
@MainActor
@Observable
final class ProjectsModel {
    enum Phase: Hashable, Sendable {
        case idle
        /// First fetch for the current filter, nothing to show yet.
        case loading
        case loaded
        /// The fetch failed and no list for the current filter is on screen.
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
            case .server: "Couldn't load your projects."
            }
        }
    }

    private(set) var phase: Phase = .idle
    private(set) var projects: [Project] = []
    /// Archived projects are fetched only when this is on (default off).
    private(set) var showsArchived = false
    /// A refresh failed while an earlier list (same filter) stays visible.
    private(set) var refreshFailure: Failure?

    private let api: any APIClient
    /// Bumped by every load and by `reset()`; an answer carrying an older ticket is dropped, so a
    /// slow response for the previous filter (or the previous account) never lands.
    private var ticket = 0
    /// The filter `projects` was fetched with, nil before the first success.
    private var loadedWithArchived: Bool?

    init(api: any APIClient) {
        self.api = api
    }

    /// Whether the list on screen matches the current filter.
    private var hasCurrentList: Bool { loadedWithArchived == showsArchived }

    func load() async {
        ticket += 1
        let current = ticket
        let includeArchived = showsArchived
        if !hasCurrentList { phase = .loading }
        do {
            let fetched = try await api.listProjects(includeArchived: includeArchived)
            guard current == ticket else { return }
            projects = fetched
            loadedWithArchived = includeArchived
            refreshFailure = nil
            phase = .loaded
        } catch {
            guard current == ticket else { return }
            let failure = Self.failure(for: error)
            if hasCurrentList {
                refreshFailure = failure
                phase = .loaded
            } else {
                refreshFailure = nil
                phase = .failed(failure)
            }
        }
    }

    /// Changes the filter and refetches; the server decides what "archived" includes.
    func setShowsArchived(_ value: Bool) async {
        guard value != showsArchived else { return }
        showsArchived = value
        refreshFailure = nil
        await load()
    }

    /// Clears per-user state on sign-out.
    func reset() {
        ticket += 1
        phase = .idle
        projects = []
        showsArchived = false
        refreshFailure = nil
        loadedWithArchived = nil
    }

    private static func failure(for error: any Error) -> Failure {
        switch error {
        case APIError.transport, is URLError: .offline
        default: .server
        }
    }
}

/// One row's copy. Reuses the onboarding card wording (`ProjectCardText`) so a project reads the
/// same in Chat and in the list. Display formatting only: no date judgements (an elapsed due
/// date is shown as-is; the server owns the day boundary).
struct ProjectRowText {
    var cardText = ProjectCardText()

    func title(_ project: Project) -> String {
        project.name
    }

    /// "Deadline" / "Every week".
    func kindLabel(_ project: Project) -> String {
        cardText.badge(ProjectCard(project: project)) ?? ""
    }

    /// Deadline: "Due Fri, Dec 4 · about 18 h in total"; every week: "4 h a week".
    func figure(_ project: Project) -> String? {
        cardText.goalLine(ProjectCard(project: project))
    }

    /// Secondary tag for anything that isn't active. `staged` never reaches the list (D23).
    func statusTag(_ project: Project) -> String? {
        switch project.status {
        case .active, .staged: nil
        case .paused: "Paused"
        case .done: "Done"
        case .archived: "Archived"
        }
    }

    func isMuted(_ project: Project) -> Bool {
        project.status == .archived
    }

    func accessibilityLabel(_ project: Project) -> String {
        var parts = [project.name]
        switch project.kind {
        case .deadline: parts.append("deadline project")
        case .perennial: parts.append("every-week project")
        }
        switch project.goal {
        case let .deadline(date, hours):
            parts.append("due \(cardText.formatDate(date))")
            parts.append("about \(cardText.formatHours(hours)) hours in total")
        case let .perennial(hours):
            parts.append("\(cardText.formatHours(hours)) hours a week")
        }
        if let tag = statusTag(project) { parts.append(tag.lowercased()) }
        return parts.joined(separator: ", ")
    }
}
