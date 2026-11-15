import Observation

/// The four tabs (design/README.md "App shell").
enum AppTab: Hashable, Sendable {
    case week, chat, projects, settings
}

/// Tab selection, so flows (onboarding → Chat, preferences → Week) can switch tabs.
@MainActor
@Observable
final class AppNavigation {
    var selectedTab: AppTab = .week
}
