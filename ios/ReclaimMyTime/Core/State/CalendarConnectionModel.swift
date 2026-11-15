import AuthenticationServices
import Foundation
import Observation
import OSLog

/// Google Calendar connection state for onboarding §3 and Settings.
///
/// `status` is persisted so "Not now" survives relaunch as a readable "Calendar not connected"
/// state. `GET /gcal/status` (`refreshStatus`) is the authority and overwrites it when reachable.
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

    private(set) var status: Status
    private(set) var connectedEmail: String?
    private(set) var callout: Callout?
    private(set) var isConnecting = false

    private let api: any APIClient
    private let defaults: UserDefaults
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "gcal")

    init(api: any APIClient, defaults: UserDefaults = .standard) {
        self.api = api
        self.defaults = defaults
        status = defaults.string(forKey: Self.statusKey).flatMap(Status.init(rawValue:)) ?? .unknown
    }

    var isConnected: Bool { status == .connected }

    /// The consent screen is shown once per user after sign-in.
    func needsConsentStep(userId: UUID) -> Bool {
        !completedUsers.contains(userId.uuidString)
    }

    func completeConsentStep(userId: UUID) {
        var users = completedUsers
        users.insert(userId.uuidString)
        defaults.set(Array(users).sorted(), forKey: Self.consentCompletedKey)
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
        defer { isConnecting = false }
        do {
            let connect = try await api.getGcalConnectUrl()
            let callbackURL = try await authenticate(connect.authorizationUrl, GcalCallbackResult.scheme)
            guard let result = GcalCallbackResult(url: callbackURL) else {
                logger.error("Unrecognised gcal callback URL")
                callout = .failed
                return false
            }
            return apply(result)
        } catch {
            if Self.isCancellation(error) { return false }
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
        do {
            let remote = try await api.getGcalStatus()
            if let connection = remote.connection {
                connectedEmail = connection.googleEmail
                setStatus(connection.status == .active ? .connected : .revoked)
            } else {
                connectedEmail = nil
                setStatus(.notConnected)
            }
        } catch {
            logger.notice("Couldn't refresh calendar status")
        }
    }

    func dismissCallout() {
        callout = nil
    }

    private var completedUsers: Set<String> {
        Set(defaults.stringArray(forKey: Self.consentCompletedKey) ?? [])
    }

    private func setStatus(_ new: Status) {
        status = new
        defaults.set(new.rawValue, forKey: Self.statusKey)
    }

    private static func isCancellation(_ error: any Error) -> Bool {
        if error is CancellationError { return true }
        if let webError = error as? ASWebAuthenticationSessionError, webError.code == .canceledLogin { return true }
        return false
    }
}
