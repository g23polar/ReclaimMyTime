import SwiftUI

/// Continue with Google (T6.0, design/onboarding.md §2): one Google consent creates the account,
/// the session and (if the calendar scopes are granted) the calendar connection. The browser hop
/// runs in the app's own `WebAuthenticationSession`; mock mode answers it with
/// `MockWebAuthenticator` so the whole flow is walkable against fixtures.
///
/// Sign in with Apple (`AuthState.signInWithApple`) stays in the tree for a future paid-membership
/// build; no UI calls it.
struct SignInView: View {
    @Environment(AppModel.self) private var model
    @Environment(AuthState.self) private var auth
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession

    static let consentCopy = "We'll ask for your Google Calendar free/busy times and permission to add a ReclaimMyTime calendar. We never see your event titles."

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
            Spacer()
            Text("Let's start with you")
                .font(.rmt.display)
                .foregroundStyle(ColorToken.textPrimary)
            Text("Sign in so your week syncs and stays yours.")
                .font(.rmt.body)
                .foregroundStyle(ColorToken.textPrimary)

            if let failure = auth.signInFailure {
                Text(failure.message)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.warning)
                    .accessibilityAddTraits(.updatesFrequently)
            }

            Button(action: signIn) {
                Text("Continue with Google")
                    .frame(maxWidth: .infinity, minHeight: HitTarget.min)
            }
            .buttonStyle(.borderedProminent)
            .disabled(auth.isSigningIn)

            Text(Self.consentCopy)
                .font(.rmt.callout)
                .foregroundStyle(ColorToken.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding(Spacing.l)
        .overlay {
            if auth.isSigningIn { ProgressView() }
        }
    }

    private func signIn() {
        let session = webAuthenticationSession
        let usesMock = model.config.usesMock
        Task {
            await auth.signInWithGoogle(using: GcalWebAuthenticator.makeForSignIn(session: session, usesMock: usesMock))
        }
    }
}
