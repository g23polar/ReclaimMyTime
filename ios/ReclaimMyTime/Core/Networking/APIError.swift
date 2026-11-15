import Foundation

/// Every failure the API client surfaces.
enum APIError: Error, Hashable, Sendable {
    /// Non-2xx with the contract's error envelope. `code` is open (D12): handle unknown codes.
    case server(code: String, message: String, details: [String: JSONValue]?, status: Int)
    /// Non-2xx whose body was not an error envelope (e.g. a proxy page).
    case unexpectedStatus(Int)
    /// The request never produced an HTTP response.
    case transport(URLError)
    /// A 2xx body (or SSE frame) that did not match the contract.
    case decoding(String)
    /// The request body could not be encoded.
    case encoding(String)
    /// The SSE stream closed without a terminal `done`/`error` event: the turn is lost; retry it.
    case streamEndedWithoutTerminalEvent

    var serverCode: String? {
        if case .server(let code, _, _, _) = self { return code }
        return nil
    }

    var isUnauthenticated: Bool { serverCode == KnownErrorCode.unauthenticated }

    var errorBody: ErrorBody? {
        if case let .server(code, message, details, _) = self {
            return ErrorBody(code: code, message: message, details: details)
        }
        return nil
    }
}

extension APIError {
    /// Maps a non-2xx response to `.server` from the envelope, else `.unexpectedStatus`.
    static func from(status: Int, body: Data, decoder: JSONDecoder) -> APIError {
        if let envelope = try? decoder.decode(ErrorEnvelope.self, from: body) {
            return .server(
                code: envelope.error.code,
                message: envelope.error.message,
                details: envelope.error.details,
                status: status
            )
        }
        return .unexpectedStatus(status)
    }

    static func decodingFailure(_ error: any Error) -> APIError {
        .decoding(String(describing: error))
    }
}
