import Foundation

/// One contracts/fixtures file (shape documented in contracts/NOTES.md "Fixtures"). Bodies are kept
/// as raw JSON `Data` so they decode through the exact same coders as live traffic.
struct Fixture: Sendable {
    struct StreamEvent: Sendable {
        let event: String
        /// Single-line JSON of the frame's `data`.
        let data: Data
    }

    let fileName: String
    let operation: String
    let method: String
    /// Path template, e.g. `/blocks/{block_id}`.
    let path: String
    let caseName: String
    let requestValid: Bool
    let responseValid: Bool
    let pathParams: [String: String]
    let query: [String: String]
    let requestHeaders: [String: String]
    let requestBody: Data?
    let status: Int
    let responseHeaders: [String: String]
    let isEventStream: Bool
    /// JSON body for non-stream responses (nil for 204/302).
    let responseBody: Data?
    /// Parsed SSE frames for `text/event-stream` responses.
    let streamEvents: [StreamEvent]

    var isSuccess: Bool { (200..<300).contains(status) }
}

enum FixtureError: Error, Equatable {
    case malformed(file: String, reason: String)
}

enum FixtureLoader {
    /// The `fixtures` folder reference bundled from `../contracts/fixtures`.
    static func bundledDirectory(in bundle: Bundle = .main) -> URL? {
        bundle.url(forResource: "fixtures", withExtension: nil)
    }

    /// Top-level `*.json` only. `negative/` holds validator self-tests and is never mock data.
    static func loadMockFixtures(from directory: URL) throws -> [Fixture] {
        try load(from: directory)
    }

    /// `negative/*.json` (fixtures that must FAIL decoding/validation).
    static func loadNegativeFixtures(from directory: URL) throws -> [Fixture] {
        try load(from: directory.appending(path: "negative", directoryHint: .isDirectory))
    }

    static func load(from directory: URL) throws -> [Fixture] {
        let urls = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        return try urls.map(parse)
    }

    static func parse(_ url: URL) throws -> Fixture {
        let name = url.lastPathComponent
        let object = try JSONSerialization.jsonObject(with: try Data(contentsOf: url), options: [.fragmentsAllowed])
        guard let root = object as? [String: Any],
              let operation = root["operation"] as? String,
              let method = root["method"] as? String,
              let path = root["path"] as? String,
              let caseName = root["case"] as? String,
              let response = root["response"] as? [String: Any],
              let status = response["status"] as? Int
        else { throw FixtureError.malformed(file: name, reason: "missing required top-level keys") }

        let request = root["request"] as? [String: Any] ?? [:]
        let isEventStream = (response["content_type"] as? String) == "text/event-stream"
        var responseBody: Data?
        var events: [Fixture.StreamEvent] = []
        if let body = response["body"] {
            if isEventStream {
                guard let frames = body as? [[String: Any]] else {
                    throw FixtureError.malformed(file: name, reason: "event-stream body must be an array of frames")
                }
                events = try frames.map { frame in
                    guard let event = frame["event"] as? String, let data = frame["data"] else {
                        throw FixtureError.malformed(file: name, reason: "frame missing event/data")
                    }
                    return Fixture.StreamEvent(event: event, data: try JSONSerialization.data(withJSONObject: data, options: [.fragmentsAllowed]))
                }
                responseBody = try JSONSerialization.data(withJSONObject: body)
            } else {
                responseBody = try JSONSerialization.data(withJSONObject: body, options: [.fragmentsAllowed])
            }
        }
        let requestBody = try request["body"].map { try JSONSerialization.data(withJSONObject: $0, options: [.fragmentsAllowed]) }

        return Fixture(
            fileName: name,
            operation: operation,
            method: method.uppercased(),
            path: path,
            caseName: caseName,
            requestValid: root["request_valid"] as? Bool ?? true,
            responseValid: root["response_valid"] as? Bool ?? true,
            pathParams: stringMap(request["path_params"]),
            query: stringMap(request["query"]),
            requestHeaders: stringMap(request["headers"]),
            requestBody: requestBody,
            status: status,
            responseHeaders: stringMap(response["headers"]),
            isEventStream: isEventStream,
            responseBody: responseBody,
            streamEvents: events
        )
    }

    private static func stringMap(_ value: Any?) -> [String: String] {
        guard let dict = value as? [String: Any] else { return [:] }
        return dict.compactMapValues { $0 as? String }
    }
}
