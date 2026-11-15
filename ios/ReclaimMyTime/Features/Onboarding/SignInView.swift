import AuthenticationServices
import SwiftUI

/// Sign in with Apple (PRD §4.2, design/onboarding.md §2). A fresh nonce is generated per attempt;
/// Apple gets its SHA-256, the server gets the raw value.
struct SignInView: View {
    @Environment(AppModel.self) private var model
    @Environment(AuthState.self) private var auth
    @Environment(\.colorScheme) private var colorScheme
    @State private var pendingNonce: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
            Spacer()
            Text("Let's start with you")
                .font(.rmt.display)
                .foregroundStyle(ColorToken.textPrimary)
            Text("Sign in so your week syncs and stays yours.")
                .font(.rmt.body)
                .foregroundStyle(ColorToken.textPrimary)

            if auth.signInFailure != nil {
                Text("Apple sign-in didn't go through. Try again.")
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.warning)
                    .accessibilityAddTraits(.updatesFrequently)
            }

            SignInWithAppleButton(.signIn, onRequest: configure, onCompletion: complete)
                .signInWithAppleButtonStyle(colorScheme == .dark ? .white : .black)
                .frame(height: 50)
                .disabled(auth.isSigningIn)

            if model.config.usesMock {
                // Mock mode: the fixture server accepts any credential, so skip Apple's sheet.
                Button("Continue with mock account") {
                    Task {
                        await auth.signInWithApple(
                            identityToken: "mock-identity-token",
                            authorizationCode: "mock-authorization-code",
                            rawNonce: Nonce.random()
                        )
                    }
                }
                .frame(maxWidth: .infinity, minHeight: HitTarget.min)
                .disabled(auth.isSigningIn)
            }

            Text("We only use your Apple ID to sign you in. We never see your password.")
                .font(.rmt.callout)
                .foregroundStyle(ColorToken.textSecondary)
            Spacer()
        }
        .padding(Spacing.l)
        .overlay {
            if auth.isSigningIn { ProgressView() }
        }
    }

    private func configure(_ request: ASAuthorizationAppleIDRequest) {
        let nonce = Nonce.random()
        pendingNonce = nonce
        request.requestedScopes = [.email]
        request.nonce = Nonce.sha256Hex(nonce)
    }

    private func complete(_ result: Result<ASAuthorization, any Error>) {
        let rawNonce = pendingNonce
        pendingNonce = nil
        switch result {
        case .success(let authorization):
            guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                  let rawNonce,
                  let tokenData = credential.identityToken,
                  let identityToken = String(data: tokenData, encoding: .utf8),
                  let codeData = credential.authorizationCode,
                  let authorizationCode = String(data: codeData, encoding: .utf8)
            else {
                auth.appleAuthorizationFailed(cancelled: false)
                return
            }
            Task {
                await auth.signInWithApple(identityToken: identityToken, authorizationCode: authorizationCode, rawNonce: rawNonce)
            }
        case .failure(let error):
            let cancelled = (error as? ASAuthorizationError)?.code == .canceled
            auth.appleAuthorizationFailed(cancelled: cancelled)
        }
    }
}
