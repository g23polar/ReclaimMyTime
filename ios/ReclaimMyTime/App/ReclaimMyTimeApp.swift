import SwiftUI

@main
struct ReclaimMyTimeApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .environment(model.auth)
                .tint(ColorToken.accent)
        }
    }
}
