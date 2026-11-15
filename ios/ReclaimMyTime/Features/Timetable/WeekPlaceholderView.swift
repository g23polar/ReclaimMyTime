import SwiftUI

/// Placeholder for the Week tab. T5.3 builds the week/day timetable from design/timetable.md.
struct WeekPlaceholderView: View {
    var body: some View {
        NavigationStack {
            ContentUnavailableView("Your week", systemImage: "calendar", description: Text("The timetable arrives in T5.3."))
                .navigationTitle("Week")
        }
    }
}
