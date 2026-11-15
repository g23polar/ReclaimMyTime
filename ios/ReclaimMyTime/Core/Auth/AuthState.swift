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
        case appleRejected
        case network
        case other
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
