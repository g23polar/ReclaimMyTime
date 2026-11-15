import SwiftUI

/// The permission pre-prompt (design/notifications.md "Permission request"), shown on the Week
/// tab after the first week is confirmed. "Turn on" opens the system prompt; "Not now" is
/// remembered.
struct NotificationPrePromptCard: View {
    @Environment(AppModel.self) private var model
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let notifications = model.notifications
        Group {
            if notifications.showsPrePrompt {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    Label {
                        Text("Want a nudge when each block starts? That's the whole point of a timetable.")
                            .font(.rmt.body)
                            .foregroundStyle(ColorToken.textPrimary)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "bell")
                            .foregroundStyle(ColorToken.accent)
                            .accessibilityHidden(true)
                    }
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: Spacing.s) { buttons }
                        VStack(alignment: .leading, spacing: Spacing.s) { buttons }
                    }
                }
                .padding(Spacing.l)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(ColorToken.surface, in: RoundedRectangle(cornerRadius: Radius.card))
                .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 1))
                .padding(.horizontal, Spacing.l)
                .padding(.top, Spacing.s)
                .transition(reduceMotion ? .opacity : .move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(Motion.standard, value: notifications.showsPrePrompt)
    }

    @ViewBuilder
    private var buttons: some View {
        let notifications = model.notifications
        Button("Turn on") {
            Task { await notifications.acceptPrePrompt() }
        }
        .buttonStyle(.borderedProminent)
        .frame(minHeight: HitTarget.min)
        Button("Not now") {
            notifications.declinePrePrompt()
        }
        .buttonStyle(.bordered)
        .frame(minHeight: HitTarget.min)
    }
}
