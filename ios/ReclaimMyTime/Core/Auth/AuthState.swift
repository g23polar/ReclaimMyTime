import Foundation
import Observation
import OSLog

/// The app root switches on `phase`. Owns sign-in / sign-out; tokens live only in `SessionStore`.
@MainActor
@Observable
final class AuthState {
    enum Phase: Equatable {
        case loading
        case signedOut
        case signedIn(StoredSession)
    }

    /// User-facing sign-in failure (design/onboarding.md §2: inline callout, no modal).
    enum SignInFailure: Equatable {
        case appleRejected
        case network
        case other
    }

    private(set) var phase: Phase = .loading
    private(set) var isSigningIn = false
    private(set) var signInFailure: SignInFailure?

    private let api: any APIClient
    private let sessionStore: SessionStore
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "auth")

    init(api: any APIClient, sessionStore: SessionStore) {
        self.api = api
        self.sessionStore = sessionStore
    }

    func bootstrap() async {
        if let session = await sessionStore.current() {
            phase = .signedIn(session)
        } else {
            phase = .signedOut
        }
    }

    /// Exchanges an Apple credential for a session. `rawNonce` is the value whose SHA-256 was sent
    /// to Apple.
    func signInWithApple(identityToken: String, authorizationCode: String, rawNonce: String) async {
        guard !isSigningIn else { return }
        isSigningIn = true
        signInFailure = nil
        defer { isSigningIn = false }
        do {
            let auth = try await api.signInWithApple(AppleSignInRequest(
                identityToken: identityToken,
                authorizationCode: authorizationCode,
                nonce: rawNonce
            ))
            let stored = try await sessionStore.save(auth)
            logger.info("Signed in (new user: \(auth.isNewUser, privacy: .public))")
            phase = .signedIn(stored)
        } catch let error as APIError {
            logger.error("Sign in failed: \(error.serverCode ?? "non-server error", privacy: .public)")
            signInFailure = switch error {
            case .server(let code, _, _, _) where code == KnownErrorCode.appleTokenInvalid || code == KnownErrorCode.appleUnavailable:
                .appleRejected
            case .transport: .network
            default: .other
            }
        } catch {
            logger.error("Sign in failed: \(String(describing: type(of: error)), privacy: .public)")
            signInFailure = .other
        }
    }

    /// Apple's own sheet failed before we had a credential (cancel is not a failure).
    func appleAuthorizationFailed(cancelled: Bool) {
        signInFailure = cancelled ? nil : .appleRejected
    }

    /// Revokes the server session (best effort) and always clears local state.
    func signOut() async {
        do {
            try await api.revokeCurrentSession()
        } catch {
            logger.notice("Server sign-out failed; clearing local session anyway")
        }
        await sessionStore.clear()
        phase = .signedOut
    }

    /// Called when any request gets 401 `unauthenticated` (the store is already cleared).
    func sessionInvalidated() {
        phase = .signedOut
    }
}
