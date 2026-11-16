import AuthenticationServices
import SwiftUI

/// Google Calendar consent (design/onboarding.md §3), shown once after sign-in, before Chat.
struct GoogleCalendarConsentView: View {
    let userId: UUID
    @Environment(AppModel.self) private var model
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession

    struct Row: Identifiable {
        let symbol: String
        let title: String
        let copy: String
        var id: String { symbol }
    }

    static let rows = [
        Row(symbol: "eye.slash", title: "We see when you're busy, not what you're doing",
            copy: "ReclaimMyTime only reads free/busy times. Event titles, notes, attendees and locations never reach our server or the AI."),
        Row(symbol: "calendar.badge.plus", title: "We add one calendar of our own",
            copy: "Your planned blocks go into a separate \"ReclaimMyTime\" calendar. Hide it, color it, delete it: your other calendars are never touched."),
        Row(symbol: "arrow.uturn.backward", title: "You can disconnect any time",
            copy: "Settings › Google Calendar. Disconnecting stops all reading and writing immediately."),
    ]

    var body: some View {
        let calendar = model.calendar
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.xl) {
                Text("Connect your calendar")
                    .font(.rmt.display)
                    .foregroundStyle(ColorToken.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                    .padding(.top, Spacing.xxl)
                ForEach(Self.rows) { row in
                    HStack(alignment: .top, spacing: Spacing.l) {
                        Image(systemName: row.symbol)
                            .font(.title2)
                            .foregroundStyle(ColorToken.accent)
                            .frame(width: 32)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Text(row.title)
                                .font(.rmt.headline)
                                .foregroundStyle(ColorToken.textPrimary)
                            Text(row.copy)
                                .font(.rmt.body)
                                .foregroundStyle(ColorToken.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: Spacing.s) {
                if let callout = calendar.callout {
                    Text(callout.message)
                        .font(.rmt.callout)
                        .foregroundStyle(callout == .accessDenied ? ColorToken.textSecondary : ColorToken.warning)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityAddTraits(.updatesFrequently)
                }
                Button {
                    Task { await connect() }
                } label: {
                    Group {
                        if calendar.isConnecting { ProgressView() } else { Text("Connect Google Calendar") }
                    }
                    .frame(maxWidth: .infinity, minHeight: HitTarget.min)
                }
                .buttonStyle(.borderedProminent)
                .disabled(calendar.isConnecting)
                Button {
                    calendar.skip()
                    finish()
                } label: {
                    Text("Not now").frame(maxWidth: .infinity, minHeight: HitTarget.min)
                }
                .disabled(calendar.isConnecting)
            }
            .padding(Spacing.l)
            .background(ColorToken.bg)
        }
        .background(ColorToken.bg)
    }

    private func connect() async {
        let session = webAuthenticationSession
        let usesMock = model.config.usesMock
        let connected = await model.calendar.connect(using: GcalWebAuthenticator.make(session: session, usesMock: usesMock))
        if connected { finish() }
    }

    private func finish() {
        model.navigation.selectedTab = .chat
        model.calendar.completeConsentStep(userId: userId)
    }
}
