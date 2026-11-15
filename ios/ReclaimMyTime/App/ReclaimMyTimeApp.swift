import SwiftUI

@main
struct ReclaimMyTimeApp: App {
    @State private var model: AppModel

    init() {
        let model = AppModel()
        // Before launch finishes: the notification delegate must be set to receive the tap that
        // launched the app.
        model.installSystemHooks()
        _model = State(initialValue: model)
    }

    var body: some Scene {
        let model = model
        WindowGroup {
            RootView()
                .environment(model)
                .environment(model.auth)
                .tint(ColorToken.accent)
        }
        .backgroundTask(.appRefresh(BackgroundTaskID.driftCheck)) {
            await model.handleBackgroundRefresh()
        }
    }
}
