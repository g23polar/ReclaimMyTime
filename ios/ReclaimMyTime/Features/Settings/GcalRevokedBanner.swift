import SwiftUI

/// One-time banner when Google revoked access (design/notifications.md §5). No push.
struct GcalRevokedBanner: View {
    @Environment(AppModel.self) private var model
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let calendar = model.calendar
        Group {
            if calendar.showsRevokedBanner {
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Image(systemName: "exclamationmark.arrow.triangle.2.circlepath")
                        .foregroundStyle(ColorToken.warning)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        Text("Google Calendar disconnected. Your plan no longer accounts for new meetings.")
                            .font(.rmt.callout)
                            .foregroundStyle(ColorToken.textPrimary)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Reconnect", action: reconnect)
                            .font(.rmt.callout.weight(.semibold))
                            .frame(minHeight: HitTarget.min)
                            .disabled(calendar.isConnecting)
                    }
                    Spacer(minLength: 0)
                    Button {
                        calendar.dismissRevokedBanner()
                    } label: {
                        Image(systemName: "xmark")
                            .frame(width: HitTarget.min, height: HitTarget.min)
                    }
                    .foregroundStyle(ColorToken.textSecondary)
                    .accessibilityLabel("Dismiss")
                }
                .padding(.horizontal, Spacing.l)
                .padding(.vertical, Spacing.xs)
                .background(ColorToken.surfaceRaised, in: RoundedRectangle(cornerRadius: Radius.card))
                .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.warning, lineWidth: 1))
                .padding(.horizontal, Spacing.l)
                .padding(.bottom, Spacing.s)
                .transition(reduceMotion ? .opacity : .move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(Motion.standard, value: calendar.showsRevokedBanner)
    }

    private func reconnect() {
        let session = webAuthenticationSession
        let usesMock = model.config.usesMock
        let calendar = model.calendar
        calendar.dismissRevokedBanner()
        Task {
            await calendar.connect { url, scheme in
                if usesMock { return MockWebAuthenticator.callbackURL() }
                return try await session.authenticate(using: url, callbackURLScheme: scheme)
            }
        }
    }
}
