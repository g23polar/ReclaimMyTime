import SwiftUI

/// Placeholder for the Chat tab. T5.2 builds the conversational intake from design/onboarding.md.
struct ChatPlaceholderView: View {
    var body: some View {
        NavigationStack {
            ContentUnavailableView("Chat", systemImage: "bubble.left.and.bubble.right", description: Text("The agent chat arrives in T5.2."))
                .navigationTitle("Chat")
        }
    }
}
