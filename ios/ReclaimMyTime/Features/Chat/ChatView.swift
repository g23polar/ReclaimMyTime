import SwiftUI

/// Chat tab (design/onboarding.md §4): bubbles newest-at-bottom, composer pinned above the banner.
struct ChatView: View {
    @Environment(AppModel.self) private var model
    @State private var editing: EditTarget?

    struct EditTarget: Identifiable {
        let entryId: UUID
        let card: ProjectCard
        var id: UUID { card.id }
    }

    private static let bottomAnchor = "chat-bottom"

    var body: some View {
        let chat = model.chat
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: Spacing.m) {
                        ForEach(chat.entries) { entry in
                            ChatEntryView(entry: entry, onEdit: { card in
                                editing = EditTarget(entryId: entry.id, card: card)
                            })
                            .id(entry.id)
                        }
                        Color.clear.frame(height: 1).id(Self.bottomAnchor)
                    }
                    .padding(.horizontal, Spacing.l)
                    .padding(.vertical, Spacing.m)
                }
                .defaultScrollAnchor(.bottom)
                .scrollDismissesKeyboard(.interactively)
                .background(ColorToken.bg)
                .onChange(of: scrollKey(chat)) {
                    // motion.stream: append only, no easing.
                    proxy.scrollTo(Self.bottomAnchor, anchor: .bottom)
                }
            }
            .pendingChangesetBanner(above: { ChatComposer() })
            .navigationTitle("Chat")
            .navigationBarTitleDisplayMode(.inline)
            .sheet(item: $editing) { target in
                ProjectEditSheet(model: ProjectEditModel(card: target.card)) { result in
                    await model.chat.applyEdit(entryId: target.entryId, result: result)
                }
            }
        }
    }

    /// Changes whenever content grows at the bottom.
    private func scrollKey(_ chat: ChatModel) -> Int {
        var hasher = Hasher()
        hasher.combine(chat.entries.count)
        if let last = chat.entries.last {
            hasher.combine(last.content)
            hasher.combine(last.changeset?.id)
        }
        return hasher.finalize()
    }
}

/// One row of the transcript.
struct ChatEntryView: View {
    let entry: ChatModel.Entry
    let onEdit: (ProjectCard) -> Void
    @Environment(AppModel.self) private var model

    var body: some View {
        switch entry.content {
        case let .agent(text, state, status):
            VStack(alignment: .leading, spacing: Spacing.s) {
                AgentBubble(text: text, state: state)
                if let status {
                    AgentStatusLine(phase: status)
                }
                if entry.changeset != nil {
                    ProjectCardStackView(entry: entry, onEdit: onEdit)
                }
            }
        case .user(let text):
            UserBubble(text: text)
        case .failure(let failure):
            ChatFailureRow(message: failure.message) { model.chat.retryFromView() }
        case .preferences:
            if let preferences = model.chat.preferences {
                PreferencesStepView(form: preferences)
            }
        }
    }
}

struct AgentBubble: View {
    let text: String
    let state: ChatModel.AgentBubbleState

    var body: some View {
        Group {
            if state == .thinking, text.isEmpty {
                TypingIndicator()
            } else {
                Text(text)
                    .font(.rmt.chat)
                    .foregroundStyle(ColorToken.textPrimary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s + 2)
        .background(ColorToken.surface, in: RoundedRectangle(cornerRadius: Radius.card))
        .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 0.5))
        .containerRelativeFrame(.horizontal, alignment: .leading) { width, _ in width * 0.85 }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(state == .thinking && text.isEmpty ? "Assistant is typing" : "Assistant: \(text)")
        .accessibilityAddTraits(state == .streaming ? .updatesFrequently : [])
    }
}

/// Bubble width hugs short text: `containerRelativeFrame` caps the slot at 85%, and the bubble
/// background sits inside the text's own size.
struct UserBubble: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.rmt.chat)
            .foregroundStyle(ColorToken.onAccent)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, Spacing.m)
            .padding(.vertical, Spacing.s + 2)
            .background(ColorToken.accent, in: RoundedRectangle(cornerRadius: Radius.card))
            .containerRelativeFrame(.horizontal, alignment: .trailing) { width, _ in width * 0.85 }
            .frame(maxWidth: .infinity, alignment: .trailing)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("You: \(text)")
    }
}

struct TypingIndicator: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: Spacing.xs) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(ColorToken.textSecondary)
                    .frame(width: 7, height: 7)
                    .phaseAnimator([0.3, 1.0]) { dot, phase in
                        dot.opacity(reduceMotion ? 0.6 : phase)
                    } animation: { _ in
                        reduceMotion ? nil : .easeInOut(duration: 0.5).delay(Double(index) * 0.15)
                    }
            }
        }
        .frame(minHeight: 20)
        .accessibilityHidden(true)
    }
}

/// SSE `status` (onboarding.md §4 "Working").
struct AgentStatusLine: View {
    let phase: AgentPhase

    var body: some View {
        HStack(spacing: Spacing.s) {
            ProgressView().controlSize(.small)
            Text(Self.copy(for: phase))
                .font(.rmt.callout)
                .foregroundStyle(ColorToken.textSecondary)
        }
        .accessibilityElement(children: .combine)
    }

    static func copy(for phase: AgentPhase) -> String {
        switch phase {
        case .staging: "Noting that down…"
        case .solving: "Checking your week…"
        }
    }
}

struct ChatFailureRow: View {
    let message: String
    let retry: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        let layout = dynamicTypeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.s)) : AnyLayout(HStackLayout(spacing: Spacing.s))
        layout {
            Label(message, systemImage: "exclamationmark.triangle")
                .font(.rmt.callout)
                .fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(ColorToken.warning)
            if !dynamicTypeSize.isAccessibilitySize { Spacer(minLength: 0) }
            Button("Retry", action: retry)
                .buttonStyle(.bordered)
                .frame(minHeight: HitTarget.min)
                .accessibilityHint("Sends your last message again")
        }
        .padding(Spacing.m)
        .background(ColorToken.surfaceRaised, in: RoundedRectangle(cornerRadius: Radius.card))
    }
}

/// Composer: vertical TextField (1–5 lines) + `arrow.up.circle.fill`, disabled while streaming.
struct ChatComposer: View {
    @Environment(AppModel.self) private var model
    @FocusState private var focused: Bool

    var body: some View {
        @Bindable var chat = model.chat
        HStack(alignment: .bottom, spacing: Spacing.s) {
            TextField("Message", text: $chat.draft, axis: .vertical)
                .font(.rmt.chat)
                .lineLimit(1...5)
                .focused($focused)
                .padding(.horizontal, Spacing.m)
                .padding(.vertical, Spacing.s)
                .background(ColorToken.surface, in: RoundedRectangle(cornerRadius: Radius.card))
                .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 1))
                .onChange(of: chat.draft) { _, new in
                    if new.count > ChatModel.maxMessageLength { chat.draft = String(new.prefix(ChatModel.maxMessageLength)) }
                }
                .onSubmit { chat.sendDraft() }
            Button {
                chat.sendDraft()
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(.title))
                    .frame(minWidth: HitTarget.min, minHeight: HitTarget.min)
            }
            .disabled(!chat.canSend)
            .accessibilityLabel("Send")
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
        .background(.bar)
    }
}

#Preview("Chat bubbles") {
    ScrollView {
        VStack(spacing: Spacing.m) {
            AgentBubble(text: ChatModel.openingMessage, state: .done)
            UserBubble(text: "A portfolio site, due mid-December.")
            AgentBubble(text: "", state: .thinking)
            AgentStatusLine(phase: .solving)
            ChatFailureRow(message: ChatModel.TurnFailure.unreachable.message) {}
        }
        .padding()
    }
    .background(ColorToken.bg)
}

#Preview("Chat bubbles AX5") {
    ScrollView {
        VStack(spacing: Spacing.m) {
            AgentBubble(text: ChatModel.openingMessage, state: .done)
            UserBubble(text: "A portfolio site, due mid-December.")
            ChatFailureRow(message: ChatModel.TurnFailure.unreachable.message) {}
        }
        .padding()
    }
    .environment(\.dynamicTypeSize, .accessibility5)
}
