import Foundation

/// Runtime configuration. One flag switches the whole app onto contract fixtures:
/// launch argument `-UseMockAPI` (or `-UseMockAPI YES`; `-UseMockAPI NO` forces live).
/// The shared scheme carries it disabled; a plain Run talks to prod.
struct AppConfig: Sendable {
    enum Backend: Sendable, Equatable {
        case mock
        case live(URL)
    }

    static let defaultLiveBaseURL = "https://reclaimmytime.fly.dev"
    /// Placeholder origin for mock requests; never resolved.
    static let mockBaseURL = "https://mock.reclaimmytime.invalid"

    let backend: Backend

    var usesMock: Bool { backend == .mock }

    var baseURL: URL {
        switch backend {
        case .mock: URL(string: Self.mockBaseURL) ?? URL(fileURLWithPath: "/")
        case .live(let url): url
        }
    }

    static func fromProcess(arguments: [String] = ProcessInfo.processInfo.arguments, defaults: UserDefaults = .standard) -> AppConfig {
        let useMock: Bool
        if defaults.object(forKey: "UseMockAPI") != nil {
            useMock = defaults.bool(forKey: "UseMockAPI")
        } else {
            useMock = arguments.contains("-UseMockAPI")
        }
        if useMock { return AppConfig(backend: .mock) }
        // Prod by default; the `APIBaseURL` user default still overrides it.
        let raw = defaults.string(forKey: "APIBaseURL") ?? Self.defaultLiveBaseURL
        return AppConfig(backend: .live(URL(string: raw) ?? URL(fileURLWithPath: "/")))
    }
}
