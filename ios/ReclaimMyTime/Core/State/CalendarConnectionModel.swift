import AuthenticationServices
import Foundation
import Observation
import OSLog

/// Google Calendar connection state for onboarding §3 and Settings.
///
/// `status` is persisted so "Not now" survives relaunch as a readable "Calendar not connected"
/// state. `GET /gcal/status` (`refreshStatus`) is the authority and overwrites it when reachable.
///
/// All of it belongs to one account on one backend: `activate(userId:)` binds the model to the
/// signed-in user (nil = signed out), clears in-memory identity, loads that user's own persisted
/// status, and bumps an epoch so results of requests started for a previous account are dropped.
@MainActor
@Observable
final class CalendarConnectionModel {
    enum Status: String, Sendable {
        case unknown
        case connected
        /// Never connected, skipped ("Not now"), or disconnected.
        case notConnected
        /// Google revoked access (`invalid_grant`): needs reconnecting.
        case revoked
    }

    /// Inline callouts mapped from an error `code` — the callback redirect's or `POST /gcal/complete`'s
    /// (onboarding.md §3). The same code shows the same callout whichever path reported it.
    enum Callout: Hashable, Sendable {
        case accessDenied
        case scopeInsufficient
        case failed

        var message: String {
            switch self {
            case .accessDenied: "No problem. You can connect later in Settings."
            case .scopeInsufficient: "Google didn't grant calendar access. Please try again and allow both permissions."
            case .failed: "Something went wrong with Google. Try again."
            }
        }
    }

    /// See `WebAuthenticator`.
    typealias Authenticator = WebAuthenticator

    static let statusKey = "gcalConnectionStatus"
    static let consentCompletedKey = "gcalConsentCompletedUsers"
    static let revokedBannerKey = "gcalRevokedBannerShown"

    private(set) var status: Status
    private(set) var connectedEmail: String?
    private(set) var callout: Callout?
    private(set) var isConnecting = false
    /// Users who finished the consent step. Stored (not computed from defaults) so RootView's
    /// routing observes `completeConsentStep`.
    private(set) var consentCompletedUsers: Set<String>
    private(set) var activeUserId: UUID?
    /// The one-time "Google Calendar disconnected" banner (design/notifications.md §5).
    private(set) var showsRevokedBanner = false
    /// The server answered 409 `gcal_reconnect_required` (a solving route or `POST /gcal/sync`)
    /// since the last successful connect or sync. Kept even when `GET /gcal/status` still says
    /// `active`: an undecryptable stored credential needs a reconnect but isn't `revoked`.
    private(set) var needsReconnect = false

    /// How a 409 `gcal_reconnect_required` raises the banner.
    enum ReconnectAnnouncement: Sendable {
        /// A change the user just tried was refused: always show the way out.
        case always
        /// Background/foreground sync: once per revocation, like §5's status-driven banner.
        case oncePerRevocation
    }

    /// Marker for a revocation first learned from a 409 (no `revoked_at` to key it by).
    static let reconnectRequiredMarker = "reconnect_required"

    private let api: any APIClient
    private let defaults: UserDefaults
    /// Separates persisted state per backend (mock vs each live host).
    let scope: String
    private var epoch = 0
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "gcal")

    init(api: any APIClient, defaults: UserDefaults = .standard, scope: String = "default") {
        self.api = api
        self.defaults = defaults
        self.scope = scope
        status = .unknown
        consentCompletedUsers = Set(defaults.stringArray(forKey: Self.consentCompletedKey) ?? [])
    }

    /// Per-account, per-backend persistence key for `status`.
    func statusKey(for userId: UUID) -> String {
        "\(Self.statusKey).\(scope).\(userId.uuidString)"
    }

    var isConnected: Bool { status == .connected }

    /// Binds the model to `userId` (nil = signed out). Idempotent for the same user.
    func activate(userId: UUID?) {
        guard userId != activeUserId else { return }
        epoch += 1
        activeUserId = userId
        connectedEmail = nil
        callout = nil
        isConnecting = false
        showsRevokedBanner = false
        needsReconnect = false
        status = userId.flatMap { defaults.string(forKey: statusKey(for: $0)) }.flatMap(Status.init(rawValue:)) ?? .unknown
    }

    /// The consent screen is shown once per user after sign-in.
    func needsConsentStep(userId: UUID) -> Bool {
        !consentCompletedUsers.contains(userId.uuidString)
    }

    func completeConsentStep(userId: UUID) {
        consentCompletedUsers.insert(userId.uuidString)
        defaults.set(Array(consentCompletedUsers).sorted(), forKey: Self.consentCompletedKey)
    }

    /// Google sign-in (T6.0) for `userId` just stored a session. Binds the model to that account
    /// and, when the same consent granted the calendar (`gcal_connected: true`), records the
    /// connection and completes the consent step so onboarding skips "Connect Google Calendar".
    /// `false` changes nothing beyond the binding: a new account still gets the consent step, and a
    /// returning one keeps whatever it had until `GET /gcal/status` (the foreground pass) says
    /// otherwise. The connected email arrives with that same status read.
    func adoptSignInGrant(userId: UUID, gcalConnected: Bool) {
        activate(userId: userId)
        guard gcalConnected else { return }
        // A fresh grant (and credential) replaces whatever needed reconnecting.
        needsReconnect = false
        callout = nil
        setStatus(.connected)
        clearRevokedBannerMarker()
        completeConsentStep(userId: userId)
    }

    /// "Not now": planning proceeds without busy data (O5).
    func skip() {
        callout = nil
        setStatus(.notConnected)
    }

    /// Runs the OAuth hand-off: `GET /gcal/connect` → browser → `authorized` callback →
    /// client-side state check → `POST /gcal/complete`. Returns true once connected.
    @discardableResult
    func connect(using authenticate: Authenticator) async -> Bool {
        guard !isConnecting else { return false }
        isConnecting = true
        callout = nil
        let started = epoch
        defer { if started == epoch { isConnecting = false } }
        do {
            let connect = try await api.getGcalConnectUrl()
            guard started == epoch else { return false }
            let callbackURL = try await authenticate(connect.authorizationUrl, GcalCallbackResult.scheme)
            guard started == epoch else { return false }
            guard let result = GcalCallbackResult(url: callbackURL) else {
                logger.error("Unrecognised gcal callback URL")
                callout = .failed
                return false
            }
            switch result {
            case .error(let code):
                applyError(code: code)
                return false
            case .authorized(let authCode, let state):
                // The callback must answer the authorization URL this app opened. A mismatch means
                // the code was minted for some other flow; never forward it to the server.
                guard let expected = connect.state, !expected.isEmpty, state == expected else {
                    logger.error("gcal callback state mismatch")
                    applyError(code: GcalCallbackResult.stateInvalidCode)
                    return false
                }
                let remote = try await api.completeGcalConnect(GcalCompleteRequest(authCode: authCode, state: state))
                guard started == epoch else { return false }
                // A fresh grant (and credential) replaces whatever needed reconnecting.
                if remote.connection?.status == .active { needsReconnect = false }
                applyRemote(remote)
                guard status == .connected else {
                    logger.error("gcal complete returned no active connection")
                    callout = .failed
                    return false
                }
                callout = nil
                return true
            }
        } catch {
            if Self.isCancellation(error) || started != epoch { return false }
            logger.error("Google connect failed")
            // `POST /gcal/complete` errors carry the same codes (and callouts) as the redirect.
            if let code = (error as? APIError)?.serverCode {
                applyError(code: code)
            } else {
                callout = .failed
            }
            return false
        }
    }

    private func applyError(code: String) {
        callout = Self.callout(forCode: code)
        if code == "gcal_access_denied", status != .connected { setStatus(.notConnected) }
    }

    static func callout(forCode code: String) -> Callout {
        switch code {
        case "gcal_access_denied": .accessDenied
        case "gcal_scope_insufficient": .scopeInsufficient
        default: .failed
        }
    }

    /// Server truth (`GET /gcal/status`). Network failures keep the persisted state.
    func refreshStatus() async {
        let started = epoch
        do {
            let remote = try await api.getGcalStatus()
            guard started == epoch else { return }
            applyRemote(remote)
        } catch {
            logger.notice("Couldn't refresh calendar status")
        }
    }

    /// A 409 `gcal_reconnect_required`: Settings shows Reconnect and the banner offers it. Ignored
    /// when signed out. (The server never sends it to a user who never connected.)
    func noteReconnectRequired(_ announcement: ReconnectAnnouncement) {
        guard let activeUserId else { return }
        needsReconnect = true
        setStatus(.revoked)
        let key = revokedBannerKey(for: activeUserId)
        switch announcement {
        case .always:
            if defaults.string(forKey: key) == nil { defaults.set(Self.reconnectRequiredMarker, forKey: key) }
            showsRevokedBanner = true
        case .oncePerRevocation:
            // Any marker means this revocation was already announced (by status or a 409).
            guard defaults.string(forKey: key) == nil else { return }
            defaults.set(Self.reconnectRequiredMarker, forKey: key)
            showsRevokedBanner = true
        }
    }

    /// `POST /gcal/sync` answered 200: whatever needed reconnecting no longer does. Status itself
    /// stays with `GET /gcal/status`.
    func noteSyncSucceeded() {
        needsReconnect = false
    }

    /// Applies a server `GcalStatus` (from `GET /gcal/status` or `POST /gcal/complete`).
    private func applyRemote(_ remote: GcalStatus) {
        if let connection = remote.connection {
            connectedEmail = connection.googleEmail
            if connection.status == .active, needsReconnect {
                // Active row, unusable credential: still a reconnect (see `needsReconnect`).
                setStatus(.revoked)
            } else if connection.status == .active {
                setStatus(.connected)
                clearRevokedBannerMarker()
            } else {
                setStatus(.revoked)
                raiseRevokedBannerOnce(revokedAt: connection.revokedAt)
            }
        } else {
            connectedEmail = nil
            setStatus(.notConnected)
        }
    }

    func dismissRevokedBanner() {
        showsRevokedBanner = false
    }

    /// Per-account, per-backend key remembering which revocation the banner already announced.
    func revokedBannerKey(for userId: UUID) -> String {
        "\(Self.revokedBannerKey).\(scope).\(userId.uuidString)"
    }

    /// Shown once per revocation: marked when raised, so a relaunch doesn't show it again. A later
    /// reconnect clears the marker, so a second revocation is announced too.
    private func raiseRevokedBannerOnce(revokedAt: Date?) {
        guard let activeUserId else { return }
        let marker = revokedAt.map { String($0.timeIntervalSince1970) } ?? "revoked"
        let key = revokedBannerKey(for: activeUserId)
        let stored = defaults.string(forKey: key)
        if stored == Self.reconnectRequiredMarker {
            // A 409 already announced this revocation; key it by `revoked_at` from now on.
            defaults.set(marker, forKey: key)
            return
        }
        guard stored != marker else { return }
        defaults.set(marker, forKey: key)
        showsRevokedBanner = true
    }

    private func clearRevokedBannerMarker() {
        guard let activeUserId else { return }
        defaults.removeObject(forKey: revokedBannerKey(for: activeUserId))
        showsRevokedBanner = false
    }

    func dismissCallout() {
        callout = nil
    }

    private func setStatus(_ new: Status) {
        status = new
        if let activeUserId { defaults.set(new.rawValue, forKey: statusKey(for: activeUserId)) }
    }

    private static func isCancellation(_ error: any Error) -> Bool {
        if error is CancellationError { return true }
        if let webError = error as? ASWebAuthenticationSessionError, webError.code == .canceledLogin { return true }
        return false
    }
}
