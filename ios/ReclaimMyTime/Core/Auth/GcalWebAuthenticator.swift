import AuthenticationServices
import SwiftUI

/// Opens an authorization URL and returns the callback URL. Production uses SwiftUI's
/// `WebAuthenticationSession` (ASWebAuthenticationSession); mock mode and tests inject their own.
typealias WebAuthenticator = @MainActor (_ url: URL, _ callbackScheme: String) async throws -> URL

/// The browser hop for both Google flows: sign-in (`SignInView`, T6.0) and the Google Calendar
/// connect flow (onboarding, Settings and the revoked banner).
///
/// SECURITY: the `reclaimmytime://gcal/callback` and `reclaimmytime://auth/google/callback` URLs
/// carry a one-time `auth_code` + `state`. They must only ever be delivered to the
/// `WebAuthenticationSession` (ASWebAuthenticationSession) the app itself started, which matches
/// `callbackURLScheme` without any Info.plist registration. Do NOT register `reclaimmytime` in
/// CFBundleURLTypes and do NOT add an `onOpenURL` handler for it: either would let a callback from
/// a flow the user didn't start in this app (e.g. a forwarded URL completed in someone else's
/// browser) be delivered to the app and attach the wrong Google account, or sign the user into
/// someone else's. The client-side `state` checks in `CalendarConnectionModel.connect` and
/// `AuthState.signInWithGoogle` are the second line of defence, not a substitute; for sign-in the
/// nonce bound into the server's state is the third.
enum GcalWebAuthenticator {
    /// The connect flow.
    @MainActor
    static func make(session: WebAuthenticationSession, usesMock: Bool) -> WebAuthenticator {
        { url, scheme in
            if usesMock { return MockWebAuthenticator.callbackURL(authorizationURL: url) }
            return try await authenticate(session: session, url: url, scheme: scheme)
        }
    }

    /// The sign-in flow.
    @MainActor
    static func makeForSignIn(session: WebAuthenticationSession, usesMock: Bool) -> WebAuthenticator {
        { url, scheme in
            if usesMock { return MockWebAuthenticator.signInCallbackURL(authorizationURL: url) }
            return try await authenticate(session: session, url: url, scheme: scheme)
        }
    }

    @MainActor
    private static func authenticate(session: WebAuthenticationSession, url: URL, scheme: String) async throws -> URL {
        // TODO(D15): `reclaimmytime` is a placeholder scheme until a bundle scheme or universal
        // link is chosen.
        try await session.authenticate(using: url, callbackURLScheme: scheme)
    }
}
