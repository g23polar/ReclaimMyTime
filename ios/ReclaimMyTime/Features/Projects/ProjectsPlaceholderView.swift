import SwiftUI

/// Placeholder for the Projects tab (T5.2/T5.3).
struct ProjectsPlaceholderView: View {
    var body: some View {
        NavigationStack {
            ContentUnavailableView("Projects", systemImage: "folder", description: Text("Your projects will appear here."))
                .navigationTitle("Projects")
        }
    }
}
