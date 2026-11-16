import Foundation

/// The two Google Calendar failures any solving route can answer (NOTES error table, T4.2): 409
/// `gcal_reconnect_required` and 503 `gcal_unavailable`, over HTTP or as an in-stream agent `error`.
/// Both fail closed: nothing was staged or applied.
///
/// The reconnect banner itself is raised by the API client's hook (`onGcalReconnectRequired`), so
/// every caller only has to show the inline message.
enum GcalSolveFailure: Hashable, Sendable {
    case reconnectRequired
    case unavailable

    init?(code: String?) {
        switch code {
        case KnownErrorCode.gcalReconnectRequired: self = .reconnectRequired
        case KnownErrorCode.gcalUnavailable: self = .unavailable
        default: return nil
        }
    }

    init?(_ error: any Error) {
        self.init(code: (error as? APIError)?.serverCode)
    }

    /// Inline copy where the change was attempted.
    var message: String {
        switch self {
        case .reconnectRequired: "Google Calendar needs reconnecting, so this change wasn't applied. Reconnect, then try again."
        case .unavailable: "Couldn't reach Google Calendar. Nothing was changed, try again."
        }
    }
}

/// Banner and review-header copy for changesets the server proposed on its own (D25). Nil for
/// `user_edit`/`agent`, which keep the diff-count summary.
enum ChangesetReasonCopy {
    static func headline(_ reason: ChangesetReason) -> String? {
        switch reason {
        // design/notifications.md §4: "Your calendar changed · Review".
        case .drift: "Your calendar changed"
        case .googleEdit: "You moved a block in Google Calendar"
        case .userEdit, .agent: nil
        }
    }
}
