import Observation

/// The five tabs (design/README.md "App shell").
enum AppTab: Hashable, Sendable {
    case week, todos, chat, projects, settings
}

/// Tab selection and the app-wide proposal review sheet, so flows (onboarding → Chat,
/// preferences → Week, a drag → review) can drive navigation.
@MainActor
@Observable
final class AppNavigation {
    var selectedTab: AppTab = .week
    /// The proposal review sheet (diff-and-infeasibility.md §1), presented once from the tab shell.
    var isReviewingProposal = false
    /// Opens the trade-off sheet when the review appears (entry from outside the review).
    var tradeOffsRequested = false

    /// Banner "Review", after a drag, or when an agent turn ends with a changeset that has a plan.
    func presentReview() {
        isReviewingProposal = true
    }
}
