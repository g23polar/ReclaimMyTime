import AuthenticationServices
import SwiftUI

/// The one `CalendarConnectionModel.Authenticator` used by onboarding, Settings and the revoked
/// banner: the browser hop for the Google Calendar connect flow.
///
/// SECURITY: the `reclaimmytime://gcal/callback` URL carries a one-time `auth_code` + `state`. It
/// must only ever be delivered to the `WebAuthenticationSession` (ASWebAuthenticationSession) the
/// app itself started, which matches `callbackURLScheme` without any Info.plist registration. Do
/// NOT register `reclaimmytime` in CFBundleURLTypes and do NOT add an `onOpenURL` handler for it:
/// either would let a callback from a flow the user didn't start in this app (e.g. a forwarded
/// connect URL completed in someone else's browser) be delivered to the app and attach the wrong
/// Google account. The client-side `state` check in `CalendarConnectionModel.connect` is the
/// second line of defence, not a substitute.
enum GcalWebAuthenticator {
    @MainActor
    static func make(session: WebAuthenticationSession, usesMock: Bool) -> CalendarConnectionModel.Authenticator {
        { url, scheme in
            if usesMock { return MockWebAuthenticator.callbackURL(authorizationURL: url) }
            // TODO(D15): `reclaimmytime` is a placeholder scheme until a bundle scheme or universal
            // link is chosen.
            return try await session.authenticate(using: url, callbackURLScheme: scheme)
        }
    }
}
