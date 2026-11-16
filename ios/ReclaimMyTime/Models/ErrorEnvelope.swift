import Foundation

/// `ErrorBody`: `code` is an open string (D12): clients must tolerate unknown codes.
struct ErrorBody: Codable, Hashable, Sendable {
    let code: String
    let message: String
    let details: [String: JSONValue]?

    init(code: String, message: String, details: [String: JSONValue]? = nil) {
        self.code = code
        self.message = message
        self.details = details
    }
}

/// `Error`: `{"error": {code, message, details?}}`.
struct ErrorEnvelope: Codable, Hashable, Sendable {
    let error: ErrorBody
}

/// Known error codes from contracts/NOTES.md "Error envelope". Not exhaustive by design.
enum KnownErrorCode {
    static let timezoneInvalid = "timezone_invalid"
    static let badRequest = "bad_request"
    static let unauthenticated = "unauthenticated"
    static let appleTokenInvalid = "apple_token_invalid"
    static let notFound = "not_found"
    static let preferencesNotSet = "preferences_not_set"
    static let noLivePlan = "no_live_plan"
    static let changesetNotPending = "changeset_not_pending"
    static let changesetStale = "changeset_stale"
    static let gcalReconnectRequired = "gcal_reconnect_required"
    static let gcalUnavailable = "gcal_unavailable"
    static let blockNotStarted = "block_not_started"
    static let blockImmutable = "block_immutable"
    static let blockSuperseded = "block_superseded"
    static let projectArchived = "project_archived"
    static let gcalNotConnected = "gcal_not_connected"
    static let validationFailed = "validation_failed"
    static let internalError = "internal_error"
    static let agentUnavailable = "agent_unavailable"
    static let appleUnavailable = "apple_unavailable"
    static let appleRevocationFailed = "apple_revocation_failed"
}

/// One element of `details.fields` for `validation_failed`.
struct ValidationFieldError: Hashable, Sendable {
    let path: String
    let message: String
}

extension ErrorBody {
    /// `details.fields` for `validation_failed`; empty when absent or malformed.
    var validationFields: [ValidationFieldError] {
        guard let fields = details?["fields"]?.arrayValue else { return [] }
        return fields.compactMap { item in
            guard let object = item.objectValue,
                  let path = object["path"]?.stringValue,
                  let message = object["message"]?.stringValue
            else { return nil }
            return ValidationFieldError(path: path, message: message)
        }
    }
}
