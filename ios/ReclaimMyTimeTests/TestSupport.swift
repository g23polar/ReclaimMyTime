import Foundation
import os
import XCTest
@testable import ReclaimMyTime

enum TestFixtures {
    /// The `fixtures` folder reference bundled into the host app from ../contracts/fixtures.
    static func directory(file: StaticString = #filePath, line: UInt = #line) throws -> URL {
        try XCTUnwrap(FixtureLoader.bundledDirectory(in: Bundle(for: BundleToken.self)) ?? FixtureLoader.bundledDirectory(),
                      "contracts/fixtures is not bundled", file: file, line: line)
    }

    static func all() throws -> [Fixture] {
        try FixtureLoader.loadMockFixtures(from: directory())
    }

    static func named(_ fileName: String) throws -> Fixture {
        try FixtureLoader.parse(directory().appending(path: fileName))
    }

    static func authSession() throws -> AuthSession {
        let fixture = try named("post_auth_apple__new_user.json")
        return try JSONCoding.makeDecoder().decode(AuthSession.self, from: XCTUnwrap(fixture.responseBody))
    }
}

/// Resolves to the host app bundle (tests are hosted).
private final class BundleToken {}

/// An isolated, test-scoped Keychain-backed session store.
func makeTestSessionStore() -> (SessionStore, KeychainStore) {
    let keychain = KeychainStore(service: "com.reclaimmytime.tests.\(UUID().uuidString)")
    return (SessionStore(keychain: keychain), keychain)
}

/// JSON-object equality (key names, nesting, values), independent of key order and of 20 vs 20.0.
func assertJSONEqual(_ lhs: Data, _ rhs: Data, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
    do {
        let a = try JSONSerialization.jsonObject(with: lhs, options: [.fragmentsAllowed]) as AnyObject
        let b = try JSONSerialization.jsonObject(with: rhs, options: [.fragmentsAllowed]) as AnyObject
        XCTAssertTrue(a.isEqual(b), "\(message)\n  got:      \(String(decoding: lhs, as: UTF8.self))\n  expected: \(String(decoding: rhs, as: UTF8.self))", file: file, line: line)
    } catch {
        XCTFail("\(message): invalid JSON \(error)", file: file, line: line)
    }
}

/// URLProtocol stub: tests register a handler that returns a response plus body chunks (several
/// chunks simulate a streamed SSE body). Every request is recorded with its body.
final class StubURLProtocol: URLProtocol {
    struct Reply: Sendable {
        var status: Int
        var headers: [String: String] = ["Content-Type": "application/json"]
        var chunks: [Data] = []
        var error: URLError?
    }

    typealias Handler = @Sendable (URLRequest, Data?) -> Reply

    private static let state = OSAllocatedUnfairLock<(handler: Handler?, requests: [(URLRequest, Data?)])>(initialState: (nil, []))

    static func install(_ handler: @escaping Handler) {
        state.withLock { $0 = (handler, []) }
    }

    static func reset() {
        state.withLock { $0 = (nil, []) }
    }

    static var requests: [(URLRequest, Data?)] {
        state.withLock { $0.requests }
    }

    static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let request = self.request
        let body = request.httpBody ?? request.httpBodyStream.map(Self.readAll)
        let handler = Self.state.withLock { state -> Handler? in
            state.requests.append((request, body))
            return state.handler
        }
        guard let handler, let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        let reply = handler(request, body)
        if let error = reply.error {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }
        guard let response = HTTPURLResponse(url: url, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: reply.headers) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        for chunk in reply.chunks {
            client?.urlProtocol(self, didLoad: chunk)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func readAll(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            guard read > 0 else { break }
            data.append(buffer, count: read)
        }
        return data
    }
}
