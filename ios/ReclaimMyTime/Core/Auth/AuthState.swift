import AuthenticationServices
import Foundation
import Observation
import OSLog
import os

/// The app root switches on `phase`. Owns sign-in / sign-out; tokens live only in `SessionStore`.
@MainActor
@Observable
final class AuthState {
    enum Phase: Equatable {
        case loading
        /// The Keychain could not be read; not the same as signed out. The root shows a retry.
        case sessionUnavailable
        case signedOut
        case signedIn(StoredSession)
    }

    /// User-facing sign-in failure (design/onboarding.md §2: inline callout, no modal).
    enum SignInFailure: Equatable {
        /// The user declined at Google (`gcal_access_denied`).
        case googleDenied
        /// `gcal_state_invalid` (including a client-side state mismatch), `gcal_exchange_failed`,
        /// an unrecognised callback, or any other Google-side code.
        case googleFailed
        /// Sign in with Apple (unused since T6.0).
        case appleRejected
        case network
        case other

        var message: String {
            switch self {
            case .googleDenied: "Google sign-in was cancelled. Try again when you're ready."
            case .googleFailed: "Google sign-in didn't go through. Try again."
            case .appleRejected: "Apple sign-in didn't go through. Try again."
            case .network: "Couldn't reach ReclaimMyTime. Check your connection and try again."
            case .other: "Something went wrong signing in. Try again."
            }
        }
    }

    private(set) var phase: Phase = .loading
    private(set) var isSigningIn = false
    private(set) var signInFailure: SignInFailure?

    private let api: any APIClient
    private let sessionStore: SessionStore
    private let revocationTimeout: Duration
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "auth")
    /// The in-flight best-effort remote revocation from the last sign-out (exposed for tests).
    private(set) var pendingRevocation: Task<Void, Never>?
    /// Runs after a Google sign-in's session is stored and BEFORE the phase flips to signed-in, so
    /// per-account state from the same consent (the calendar connection) is in place when RootView
    /// routes on the new phase. Wired by `AppModel`.
    var onGoogleSignIn: (@MainActor (_ session: StoredSession, _ gcalConnected: Bool) -> Void)?

    init(api: any APIClient, sessionStore: SessionStore, revocationTimeout: Duration = .seconds(10)) {
        self.api = api
        self.sessionStore = sessionStore
        self.revocationTimeout = revocationTimeout
    }

    /// Reads the stored session. A Keychain read failure yields `.sessionUnavailable` (retryable),
    /// never `.signedOut`, so a transient lock state cannot look like a sign-out.
    func bootstrap() async {
        do {
            if let session = try await sessionStore.current() {
                phase = .signedIn(session)
            } else {
                phase = .signedOut
            }
        } catch {
            logger.error("Session unavailable: keychain read failed")
            phase = .sessionUnavailable
        }
    }

    /// Google sign-in (T6.0): `POST /auth/google/start` with a fresh nonce's hash → the returned
    /// authorization URL in `authenticate` (ASWebAuthenticationSession) → `authorized` callback →
    /// client-side state check → `POST /auth/google/complete` with the raw nonce → Keychain.
    ///
    /// A callback whose `state` differs from the opened URL's is `gcal_state_invalid` and never
    /// reaches the server. Cancelling the browser sheet is not a failure.
    /// - Parameter nonce: the raw nonce; a fresh one per attempt (injectable for tests only).
    func signInWithGoogle(using authenticate: WebAuthenticator, nonce: String = Nonce.googleRaw()) async {
        guard !isSigningIn else { return }
        isSigningIn = true
        signInFailure = nil
        defer { isSigningIn = false }
        do {
            let start = try await api.startGoogleSignIn(GoogleSignInStartRequest(nonceHash: Nonce.sha256Base64URL(nonce)))
            let callbackURL = try await authenticate(start.authorizationUrl, GcalCallbackResult.scheme)
            guard let result = GcalCallbackResult(url: callbackURL, route: .signIn) else {
                logger.error("Unrecognised Google sign-in callback URL")
                signInFailure = .googleFailed
                return
            }
            switch result {
            case .error(let code):
                logger.notice("Google sign-in callback error: \(code, privacy: .public)")
                signInFailure = Self.failure(forGoogleCode: code)
            case .authorized(let authCode, let state):
                // The callback must answer the authorization URL this attempt opened. A mismatch
                // means the code was minted for some other flow; never forward it to the server.
                guard let expected = start.state, !expected.isEmpty, state == expected else {
                    logger.error("Google sign-in callback state mismatch")
                    signInFailure = Self.failure(forGoogleCode: GcalCallbackResult.stateInvalidCode)
                    return
                }
                let signedIn = try await api.completeGoogleSignIn(GoogleSignInCompleteRequest(authCode: authCode, state: state, nonce: nonce))
                let stored = try await sessionStore.save(signedIn.auth)
                logger.info("Signed in with Google (new user: \(signedIn.auth.isNewUser, privacy: .public), calendar: \(signedIn.gcalConnected, privacy: .public))")
                onGoogleSignIn?(stored, signedIn.gcalConnected)
                phase = .signedIn(stored)
            }
        } catch {
            if Self.isCancellation(error) { return }
            if let apiError = error as? APIError {
                logger.error("Google sign-in failed: \(apiError.serverCode ?? "non-server error", privacy: .public)")
                signInFailure = switch apiError {
                case .server(let code, _, _, _) where code.hasPrefix("gcal_"): Self.failure(forGoogleCode: code)
                case .transport: .network
                default: .other
                }
            } else {
                logger.error("Google sign-in failed: \(String(describing: type(of: error)), privacy: .public)")
                signInFailure = .other
            }
        }
    }

    /// Callback and `POST /auth/google/complete` codes map to the same failure.
    static func failure(forGoogleCode code: String) -> SignInFailure {
        code == "gcal_access_denied" ? .googleDenied : .googleFailed
    }

    private static func isCancellation(_ error: any Error) -> Bool {
        if error is CancellationError { return true }
        if let webError = error as? ASWebAuthenticationSessionError, webError.code == .canceledLogin { return true }
        return false
    }

    /// Exchanges an Apple credential for a session (kept but unused since T6.0; no UI calls it). `rawNonce` is the value whose SHA-256 was sent
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

    /// Local-first sign-out: the stored session is cleared and the phase flips immediately. Remote
    /// revocation then runs detached, best effort, bounded by `revocationTimeout`; it never blocks
    /// the UI and its outcome (including a 401) cannot affect a later session.
    func signOut() async {
        var token: String?
        if case .signedIn(let session) = phase { token = session.token }
        await sessionStore.clear()
        phase = .signedOut
        signInFailure = nil
        guard let token else { return }
        let api = self.api
        let timeout = revocationTimeout
        let logger = self.logger
        // The revoke runs as its own unstructured task; the race below never awaits it, so the
        // timeout bound holds even if the revoke ignores cancellation.
        let revoke = Task.detached(priority: .utility) { () -> Bool in
            do {
                try await api.revokeSession(token: token)
                return true
            } catch {
                return false
            }
        }
        pendingRevocation = Task.detached(priority: .utility) {
            let once = OnceFlag()
            let revoked = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
                Task.detached(priority: .utility) {
                    let result = await revoke.value
                    if once.claim() { continuation.resume(returning: result) }
                }
                Task.detached(priority: .utility) {
                    try? await Task.sleep(for: timeout)
                    if once.claim() {
                        revoke.cancel()
                        continuation.resume(returning: false)
                    }
                }
            }
            if !revoked { logger.notice("Remote session revocation failed or timed out; local session already cleared") }
        }
    }

    /// Called when a request carrying `token` got 401 `unauthenticated` (the store was already
    /// compare-and-cleared). Acts only if that is still the current session: a notification that
    /// arrives after a replacement sign-in is ignored.
    func sessionInvalidated(token: String?) {
        guard case .signedIn(let session) = phase, session.token == token else { return }
        phase = .signedOut
    }
}

/// Resumes a continuation exactly once across racing tasks.
private final class OnceFlag: Sendable {
    private let claimed = OSAllocatedUnfairLock(initialState: false)

    func claim() -> Bool {
        claimed.withLock { done in
            if done { return false }
            done = true
            return true
        }
    }
}
