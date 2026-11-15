import SwiftUI

/// "You're in a new time zone" (design/notifications.md §3), presented on the Week tab.
struct TimezoneChangeSheet: View {
    let prompt: TimezoneMonitor.Prompt
    @Environment(AppModel.self) private var model

    var body: some View {
        let timezone = model.timezone
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                Image(systemName: "globe")
                    .font(.title)
                    .foregroundStyle(ColorToken.accent)
                    .accessibilityHidden(true)
                Text(prompt.title)
                    .font(.rmt.title)
                    .foregroundStyle(ColorToken.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Text(prompt.message)
                    .font(.rmt.body)
                    .foregroundStyle(ColorToken.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let error = timezone.replanError {
                    Text(error)
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.warning)
                }
                VStack(spacing: Spacing.s) {
                    Button {
                        let preferences = model.timetable.preferences
                        Task { await timezone.replan(current: preferences) }
                    } label: {
                        HStack(spacing: Spacing.s) {
                            if timezone.isReplanning { ProgressView().tint(ColorToken.onAccent) }
                            Text("Re-plan my week")
                        }
                        .frame(maxWidth: .infinity, minHeight: HitTarget.min)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(timezone.isReplanning)
                    Button {
                        timezone.keepAsIs()
                    } label: {
                        Text("Keep it as is").frame(maxWidth: .infinity, minHeight: HitTarget.min)
                    }
                    .buttonStyle(.bordered)
                    .disabled(timezone.isReplanning)
                }
            }
            .padding(Spacing.l)
        }
        .background(ColorToken.bg)
        .presentationDetents([.medium, .large])
    }
}
