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

    /// Inline callouts mapped from the callback's `code` (onboarding.md §3).
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

    /// Opens the authorization URL and returns the callback URL. Production uses SwiftUI's
    /// `WebAuthenticationSession` (ASWebAuthenticationSession); mock mode and tests inject their own.
    typealias Authenticator = @MainActor (_ url: URL, _ callbackScheme: String) async throws -> URL

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

    /// "Not now": planning proceeds without busy data (O5).
    func skip() {
        callout = nil
        setStatus(.notConnected)
    }

    /// Runs the OAuth hand-off. Returns true when the callback reports `connected`.
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
            return apply(result)
        } catch {
            if Self.isCancellation(error) || started != epoch { return false }
            logger.error("Google connect failed")
            callout = .failed
            return false
        }
    }

    /// Applies a callback result (also the deep-link entry point if the app is opened via the
    /// scheme). Returns true when connected.
    @discardableResult
    func apply(_ result: GcalCallbackResult) -> Bool {
        switch result {
        case .connected:
            callout = nil
            setStatus(.connected)
            clearRevokedBannerMarker()
            return true
        case .error(let code):
            callout = Self.callout(forCode: code)
            if code == "gcal_access_denied", status != .connected { setStatus(.notConnected) }
            return false
        }
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
            if let connection = remote.connection {
                connectedEmail = connection.googleEmail
                setStatus(connection.status == .active ? .connected : .revoked)
                if connection.status == .active {
                    clearRevokedBannerMarker()
                } else {
                    raiseRevokedBannerOnce(revokedAt: connection.revokedAt)
                }
            } else {
                connectedEmail = nil
                setStatus(.notConnected)
            }
        } catch {
            logger.notice("Couldn't refresh calendar status")
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
        guard defaults.string(forKey: key) != marker else { return }
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
