import Foundation

/// The seam between the typed client and the wire. `URLSessionTransport` is the real network;
/// `MockTransport` serves contract fixtures. The client's header, decoding and error-envelope logic
/// is identical for both.
protocol HTTPTransport: Sendable {
    /// One request/response with a fully buffered body.
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse)
    /// A streaming response (SSE). Chunks may split anywhere, including inside a UTF-8 sequence.
    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>)
}

struct URLSessionTransport: HTTPTransport {
    let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        let stream = AsyncThrowingStream<Data, any Error> { continuation in
            let task = Task {
                do {
                    // AsyncBytes is internally buffered; regroup into newline-terminated chunks so
                    // the parser gets whole frames promptly without per-byte overhead.
                    var buffer = Data()
                    for try await byte in bytes {
                        buffer.append(byte)
                        if byte == UInt8(ascii: "\n") || buffer.count >= 4096 {
                            continuation.yield(buffer)
                            buffer.removeAll(keepingCapacity: true)
                        }
                    }
                    if !buffer.isEmpty { continuation.yield(buffer) }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
        return (http, stream)
    }
}
