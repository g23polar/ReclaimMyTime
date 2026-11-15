import Foundation
import Observation

/// Composition root: builds the transport (mock or network), the typed client, the session store
/// and auth state, and wires 401 handling back into auth.
@MainActor
@Observable
final class AppModel {
    let config: AppConfig
    let api: any APIClient
    let auth: AuthState
    let palette: ProjectPaletteStore

    init(config: AppConfig = .fromProcess(), sessionStore: SessionStore = SessionStore()) {
        self.config = config
        let transport: any HTTPTransport = config.usesMock ? MockTransport.bundled() : URLSessionTransport()
        let relay = SessionInvalidationRelay()
        let api = URLSessionAPIClient(
            baseURL: config.baseURL,
            transport: transport,
            sessionStore: sessionStore,
            onUnauthenticated: { await relay.notify() }
        )
        self.api = api
        self.auth = AuthState(api: api, sessionStore: sessionStore)
        self.palette = ProjectPaletteStore()
        relay.auth = auth
    }
}

/// Breaks the client ↔ auth construction cycle without a strong reference.
@MainActor
final class SessionInvalidationRelay {
    weak var auth: AuthState?

    func notify() {
        auth?.sessionInvalidated()
    }
}
