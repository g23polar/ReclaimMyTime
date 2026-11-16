import Foundation

/// Typed client for contracts/openapi.yaml: one method per operationId. `gcalCallback` has no method
/// because the browser hits it, not the app (see `GcalCallbackResult`). Staged writes return the
/// pending `Changeset` (202); nothing is applied until `confirmChangeset`.
protocol APIClient: Sendable {
    // auth
    func signInWithApple(_ request: AppleSignInRequest) async throws -> AuthSession
    func getMe() async throws -> User
    func deleteMe() async throws
    func revokeAllSessions() async throws
    func revokeCurrentSession() async throws
    /// Revokes the session identified by `token` (not the stored one): used by local-first
    /// sign-out after the Keychain is already cleared.
    func revokeSession(token: String) async throws

    // projects
    func listProjects(includeArchived: Bool) async throws -> [Project]
    func createProject(_ draft: ProjectDraft) async throws -> Changeset
    func getProject(id: UUID) async throws -> Project
    /// 200 + Project for a name-only edit (applied directly); 202 + Changeset otherwise.
    func updateProject(id: UUID, patch: ProjectPatch) async throws -> UpdateProjectResult
    func archiveProject(id: UUID) async throws -> Changeset

    // preferences
    func getPreferences() async throws -> UserPreferences
    func putPreferences(_ preferences: UserPreferences) async throws -> Changeset

    // exceptions
    func listExceptions(includePast: Bool) async throws -> [AvailabilityException]
    func createException(_ draft: ExceptionDraft) async throws -> Changeset
    func getException(id: UUID) async throws -> AvailabilityException
    func deleteException(id: UUID) async throws -> Changeset

    // plans & blocks
    func getCurrentPlan() async throws -> WeekPlan
    func getPlanDiff(planId: UUID, against: UUID) async throws -> PlanDiff
    func patchBlock(id: UUID, action: BlockAction) async throws -> PatchBlockResult

    // pins
    func listPins(includePast: Bool) async throws -> [BlockPin]
    func deletePin(id: UUID) async throws -> Changeset

    // agent: pre-stream failures arrive as a thrown `APIError` from the stream.
    func postAgentMessage(_ request: AgentMessageRequest) -> AsyncThrowingStream<AgentEvent, any Error>

    // changesets
    func confirmChangeset(id: UUID) async throws -> Changeset
    func rejectChangeset(id: UUID) async throws -> Changeset

    // gcal
    func getGcalConnectUrl() async throws -> GcalConnectUrl
    func getGcalStatus() async throws -> GcalStatus
    /// `POST /gcal/complete`: finishes the connect flow with an `authorized` callback's code and
    /// state. Errors: 400 `gcal_state_invalid`, 422 `gcal_scope_insufficient`, 502 `gcal_exchange_failed`.
    func completeGcalConnect(_ request: GcalCompleteRequest) async throws -> GcalStatus
    func disconnectGcal() async throws
}

extension APIClient {
    func listProjects() async throws -> [Project] { try await listProjects(includeArchived: false) }
    func listExceptions() async throws -> [AvailabilityException] { try await listExceptions(includePast: false) }
    func listPins() async throws -> [BlockPin] { try await listPins(includePast: false) }
}

/// A request description, independent of base URL and auth.
struct Endpoint: Sendable {
    enum Method: String, Sendable { case get = "GET", post = "POST", put = "PUT", patch = "PATCH", delete = "DELETE" }

    let method: Method
    let path: String
    var query: [URLQueryItem] = []
    var body: Data?
    /// False only for `POST /auth/apple`.
    var requiresAuth = true
    /// Overrides the stored session's token for this request.
    var bearerToken: String?
    var acceptsEventStream = false
}

extension UUID {
    /// Lowercase canonical form for URL paths (Foundation's `uuidString` is uppercase).
    var pathComponent: String { uuidString.lowercased() }
}
