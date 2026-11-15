import Foundation
import Observation
import OSLog

/// The Chat tab's state (design/onboarding.md §4). Unidirectional: views render `entries`; intents
/// (`send`, `retry`, `confirm`, `applyEdit`, `submitPreferences`) are async and mutate on the main
/// actor.
///
/// Rules this enforces (CLAUDE.md 1, 5, 6):
/// - The LLM is only called from `submit`/`retry`, i.e. a user action. The opening message is
///   static client copy.
/// - The client never computes schedules: it renders `done.reply` and the staged changeset, and
///   every change becomes a pending changeset the user confirms.
/// - The server is stateless: the full transcript is sent every turn.
@MainActor
@Observable
final class ChatModel {
    enum AgentBubbleState: Hashable, Sendable {
        /// Sent; no event yet (typing indicator).
        case thinking
        /// `delta` text arriving.
        case streaming
        case done
        /// `done` with `outcome: refused`: the decline text, nothing else.
        case refused
    }

    struct Entry: Identifiable, Hashable, Sendable {
        enum Content: Hashable, Sendable {
            case agent(text: String, state: AgentBubbleState, status: AgentPhase?)
            case user(text: String)
            /// "Couldn't reach the assistant." + Retry.
            case failure
            /// The in-chat preferences cards.
            case preferences
        }

        let id: UUID
        var content: Content
        /// The changeset whose project cards render under this entry. Replaced in place when an
        /// edit supersedes it.
        var changeset: Changeset?

        init(id: UUID = UUID(), content: Content, changeset: Changeset? = nil) {
            self.id = id
            self.content = content
            self.changeset = changeset
        }
    }

    static let openingMessage = "Hi! Let's figure out what you want to make time for. What's one project on your mind right now: something with a deadline, or something you want to do every week?"
    static let preferencesQuestionHours = "When can I schedule things?"
    static let preferencesQuestionDiversity = "Do you like themed days where you hammer one thing, or variety in every day?"
    static let maxTranscriptMessages = 200
    static let maxMessageLength = 8000

    private(set) var entries: [Entry]
    /// What the server sees: user turns and `done.reply` texts only (no static copy).
    private(set) var transcript: [TranscriptMessage] = []
    var draft = ""
    private(set) var isStreaming = false
    private(set) var preferences: PreferencesFormModel?

    private let api: any APIClient
    private let pendingStore: PendingChangesetStore
    private let navigation: AppNavigation
    private let directory: ProjectDirectory
    private let timeZoneIdentifier: @Sendable () -> String
    private let calendar: Calendar
    private var composerTask: Task<Void, Never>?
    /// Bumped by `reset()` (sign-out). Every await re-checks it so an outstanding turn, edit or
    /// preferences submit from a previous session never writes into this one.
    private var generation = 0
    private let logger = Logger(subsystem: "com.reclaimmytime.app", category: "chat")

    init(
        api: any APIClient,
        pendingStore: PendingChangesetStore,
        navigation: AppNavigation,
        directory: ProjectDirectory,
        timeZoneIdentifier: @escaping @Sendable () -> String = { TimeZone.current.identifier },
        calendar: Calendar = .current
    ) {
        self.api = api
        self.pendingStore = pendingStore
        self.navigation = navigation
        self.directory = directory
        self.timeZoneIdentifier = timeZoneIdentifier
        self.calendar = calendar
        entries = [Self.openingEntry()]
    }

    var canSend: Bool {
        !isStreaming && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var hasFailedTurn: Bool {
        entries.last?.content == .failure
    }

    // MARK: intents

    /// Composer send (fire and forget from the view).
    func sendDraft() {
        guard canSend else { return }
        let text = draft
        draft = ""
        composerTask = Task { await self.submit(text) }
    }

    /// Composer Retry (fire and forget from the view).
    func retryFromView() {
        guard !isStreaming else { return }
        composerTask = Task { await self.retry() }
    }

    /// Sends one user message and runs the turn to its terminal event.
    func submit(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !isStreaming, !trimmed.isEmpty else { return }
        let content = String(trimmed.prefix(Self.maxMessageLength))
        entries.removeAll { $0.content == .failure }
        entries.append(Entry(content: .user(text: content)))
        transcript.append(TranscriptMessage(role: .user, content: content))
        await runTurn()
    }

    /// Re-sends the same transcript after a failed turn. This is a new turn, not a resume (the
    /// endpoint is not idempotent and has no resume, NOTES "SSE").
    func retry() async {
        guard !isStreaming, hasFailedTurn, transcript.last?.role == .user else { return }
        entries.removeAll { $0.content == .failure }
        await runTurn()
    }

    /// "Looks right" on a card stack.
    func confirmCards(entryId: UUID) async {
        guard let changeset = entry(entryId)?.changeset else { return }
        await pendingStore.confirm(id: changeset.id)
    }

    /// The Edit sheet's PATCH returned a new pending changeset: it supersedes the old one and the
    /// cards under `entryId` update in place.
    func applyEdit(entryId: UUID, changeset: Changeset) async {
        guard let index = entries.firstIndex(where: { $0.id == entryId }) else { return }
        directory.register(changeset)
        guard pendingStore.stage(changeset) else { return }
        entries[index].changeset = changeset
        await directory.ensureBases(for: changeset)
    }

    /// Cards for an entry's changeset.
    func cards(for entry: Entry) -> [ProjectCard] {
        guard let changeset = entry.changeset else { return [] }
        return ProjectCardDerivation.cards(from: changeset, base: { directory.card(for: $0) })
    }

    /// Submit on the preferences cards: `PUT /preferences` → 202 → pending; with a plan, go to Week.
    @discardableResult
    func submitPreferences() async -> Changeset? {
        guard let preferences else { return nil }
        let started = generation
        let changeset = await preferences.submit(
            api: api,
            timeZoneIdentifier: timeZoneIdentifier(),
            weekStart: PreferencesFormModel.isoWeekStart(calendar: calendar)
        )
        guard started == generation, let changeset else { return nil }
        // A newer proposal (e.g. an agent turn) may have been adopted while the PUT was in flight;
        // the server expired this one when it created that one (D4). Not adopted = not submitted.
        guard pendingStore.stage(changeset) else {
            preferences.reopen()
            return nil
        }
        if changeset.planId != nil {
            navigation.selectedTab = .week
            navigation.presentReview()
        }
        return changeset
    }

    /// Wired to `PendingChangesetStore.onConfirmed`. Confirming the onboarding projects (a
    /// changeset with no plan, i.e. preferences don't exist yet, NOTES "Staging model") opens the
    /// preferences step. Static client copy: no LLM call.
    func changesetConfirmed(_ changeset: Changeset) {
        let createsProject = changeset.staged.contains { $0.kind == .projectCreated }
        if changeset.planId == nil, createsProject {
            showPreferencesPrompt()
        }
    }

    /// Wired to `PendingChangesetStore.onDiscarded`: a rejected/expired preferences proposal lets
    /// the user change their answers, unless its replacement carries the preferences forward.
    func changesetDiscarded(_ changeset: Changeset, replacement: Changeset? = nil) {
        let carriesPreferences: (Changeset) -> Bool = { $0.staged.contains { $0.kind == .preferencesUpdated } }
        guard carriesPreferences(changeset) else { return }
        if let replacement, carriesPreferences(replacement) { return }
        preferences?.reopen()
    }

    func showPreferencesPrompt() {
        guard preferences == nil else { return }
        preferences = PreferencesFormModel()
        entries.append(Entry(content: .preferences))
    }

    /// Sign-out: chat is ephemeral.
    func reset() {
        generation += 1
        composerTask?.cancel()
        composerTask = nil
        entries = [Self.openingEntry()]
        transcript = []
        draft = ""
        isStreaming = false
        preferences = nil
    }

    #if DEBUG
    /// Mock-mode demo only: shows a staged changeset's cards under a static agent bubble without
    /// calling the agent (the fixtures have no onboarding agent turn).
    func debugAttach(changeset: Changeset, text: String) {
        directory.register(changeset)
        pendingStore.stage(changeset)
        entries.append(Entry(content: .agent(text: text, state: .done, status: nil), changeset: changeset))
    }
    #endif

    // MARK: turn

    private func runTurn() async {
        let started = generation
        isStreaming = true
        defer { if started == generation { isStreaming = false } }
        let agentEntry = Entry(content: .agent(text: "", state: .thinking, status: nil))
        entries.append(agentEntry)
        let request = AgentMessageRequest(messages: Self.trimmedForRequest(transcript))
        var streamed = ""
        var status: AgentPhase?
        do {
            for try await event in api.postAgentMessage(request) {
                guard started == generation else { return }
                switch event {
                case .delta(let text):
                    streamed += text
                    status = nil
                    update(agentEntry.id, .agent(text: streamed, state: .streaming, status: nil))
                case .status(let phase):
                    status = phase
                    update(agentEntry.id, .agent(text: streamed, state: streamed.isEmpty ? .thinking : .streaming, status: status))
                case .done(let done):
                    await finish(agentEntry.id, done)
                    return
                case .error(let body):
                    fail(agentEntry.id, code: body.code)
                    return
                }
            }
            guard started == generation else { return }
            fail(agentEntry.id, code: nil)
        } catch is CancellationError {
            guard started == generation else { return }
            entries.removeAll { $0.id == agentEntry.id }
        } catch {
            guard started == generation else { return }
            fail(agentEntry.id, code: (error as? APIError)?.serverCode)
        }
    }

    private func finish(_ id: UUID, _ done: AgentDone) async {
        // `done.reply` is authoritative: it replaces the streamed text (also covers a late refusal).
        let state: AgentBubbleState = done.outcome == .refused ? .refused : .done
        let changeset = done.outcome == .refused ? nil : done.changeset
        guard let index = entries.firstIndex(where: { $0.id == id }) else { return }
        entries[index].content = .agent(text: done.reply, state: state, status: nil)
        if !done.reply.isEmpty {
            transcript.append(TranscriptMessage(role: .assistant, content: String(done.reply.prefix(Self.maxMessageLength)), changesetId: changeset?.id))
        }
        guard let changeset else { return }
        directory.register(changeset)
        // A proposal with a plan opens review straight away (diff-and-infeasibility.md §1); plan-less
        // onboarding proposals are reviewed through the inline cards.
        if pendingStore.stage(changeset), changeset.planId != nil {
            navigation.presentReview()
        }
        if ProjectCardDerivation.hasProjectCards(changeset) {
            entries[index].changeset = changeset
            await directory.ensureBases(for: changeset)
        }
    }

    /// On `error` (or a pre-stream failure) nothing was staged: discard the streamed text and show
    /// the Retry row. The user's message stays in the transcript, so Retry resends it.
    private func fail(_ id: UUID, code: String?) {
        logger.notice("Agent turn failed: \(code ?? "transport", privacy: .public)")
        entries.removeAll { $0.id == id }
        entries.append(Entry(content: .failure))
    }

    private func update(_ id: UUID, _ content: Entry.Content) {
        guard let index = entries.firstIndex(where: { $0.id == id }) else { return }
        entries[index].content = content
    }

    private func entry(_ id: UUID) -> Entry? {
        entries.first { $0.id == id }
    }

    private static func openingEntry() -> Entry {
        Entry(content: .agent(text: openingMessage, state: .done, status: nil))
    }

    /// Contract limits (D14): at most 200 messages, oldest trimmed first; 8000 chars each.
    static func trimmedForRequest(_ transcript: [TranscriptMessage]) -> [TranscriptMessage] {
        transcript.suffix(maxTranscriptMessages).map { message in
            var message = message
            if message.content.count > maxMessageLength {
                message.content = String(message.content.prefix(maxMessageLength))
            }
            return message
        }
    }
}
