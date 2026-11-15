#if DEBUG
import Foundation

/// DEBUG-only screenshot/QA helper for `-DemoScenario week|day|review|tradeoff` (mock mode).
/// The fixture story has no live `kind: pin` row, so `GET /pins` gets one extra pin that exactly
/// matches the live plan's Wed 7–8 AM Workout block (so the G2 join marks it pinned). Everything
/// else is served unchanged by the fixture mock. No scheduling happens here.
actor DemoTransport: HTTPTransport {
    let inner: MockTransport

    static var demoPinJSON: [String: Any] { [
        "id": "d3b8f6a1-9c2e-4a7d-b5f0-6e1c8a2d4b01",
        "project_id": "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b02",
        "span": ["start": "2026-11-18T15:00:00Z", "end": "2026-11-18T16:00:00Z"],
        "kind": "pin",
        "created_from_block_id": "55739f43-47ae-598d-96ab-7909a7fbbb63",
        "created_at": "2026-11-17T18:20:00Z",
    ] }

    /// Tue 2026-11-17 10:30 America/Los_Angeles: the fixture story's "now".
    static let demoNow = Date(timeIntervalSince1970: 1_794_940_200)

    init(inner: MockTransport) {
        self.inner = inner
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await inner.data(for: request)
        guard request.httpMethod == "GET", request.url?.path() == "/pins", response.statusCode == 200,
              var object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              var pins = object["pins"] as? [[String: Any]]
        else { return (data, response) }
        pins.insert(Self.demoPinJSON, at: 0)
        object["pins"] = pins
        return (try JSONSerialization.data(withJSONObject: object), response)
    }

    func stream(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<Data, any Error>) {
        try await inner.stream(for: request)
    }
}
#endif
