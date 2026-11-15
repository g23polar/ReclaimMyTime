import XCTest
@testable import ReclaimMyTime

@MainActor
final class ChatStreamingTests: XCTestCase {
    private func lastAgent(_ chat: ChatModel) -> (String, ChatModel.AgentBubbleState, AgentPhase?)? {
        for entry in chat.entries.reversed() {
            if case let .agent(text, state, status) = entry.content { return (text, state, status) }
        }
        return nil
    }

    /// Replays the `laptop` SSE fixture with gaps between frames and samples every distinct state.
    func testLaptopTurnWalksThinkingStreamingStatusDone() async throws {
        let h = try T52Harness(frameDelay: .milliseconds(80))
        let turn = Task { await h.chat.submit("I don't have my laptop today or tomorrow.") }
        var seen: [ChatModel.Entry.Content] = []
        while !turn.isCancelled {
            if let last = h.chat.entries.last?.content, seen.last != last { seen.append(last) }
            if !h.chat.isStreaming, seen.count > 1 { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        await turn.value

        XCTAssertTrue(seen.contains(.agent(text: "", state: .thinking, status: nil)), "thinking first: \(seen)")
        XCTAssertTrue(seen.contains(.agent(text: "Got it: no laptop from now through Wednesday night.", state: .streaming, status: nil)))
        XCTAssertTrue(seen.contains { if case .agent(_, .streaming, .staging?) = $0 { true } else { false } }, "staging status line")
        XCTAssertTrue(seen.contains { if case .agent(_, .streaming, .solving?) = $0 { true } else { false } }, "solving status line")

        let done = try T52Fixtures.agentDone("post_agent_message__laptop.json")
        let final = try XCTUnwrap(lastAgent(h.chat))
        XCTAssertEqual(final.0, done.reply, "done.reply replaces the streamed text")
        XCTAssertEqual(final.1, .done)
        XCTAssertNil(final.2, "status line clears on done")
        XCTAssertEqual(h.pending.pending?.id, done.changeset?.id, "done + changeset → pending (banner)")
        XCTAssertNil(h.chat.entries.last?.changeset, "an exception-only changeset has no project cards")
        XCTAssertFalse(h.chat.isStreaming)
    }

    func testRefusalShowsDeclineAndChangesNothing() async throws {
        let h = try T52Harness()
        await h.mock.setVariant("refusal", for: "postAgentMessage")
        await h.chat.submit("Find someone's home address")
        let final = try XCTUnwrap(lastAgent(h.chat))
        XCTAssertEqual(final.1, .refused)
        XCTAssertEqual(final.0, try T52Fixtures.agentDone("post_agent_message__refusal.json").reply)
        XCTAssertNil(h.pending.pending)
        XCTAssertNil(h.chat.transcript.last?.changesetId)
        XCTAssertEqual(h.chat.transcript.map(\.role), [.user, .assistant])
    }

    func testStreamErrorDiscardsTextAndOffersRetryAsNewTurn() async throws {
        let h = try T52Harness()
        await h.mock.setVariant("upstream_error", for: "postAgentMessage")
        await h.chat.submit("I don't have my laptop today or tomorrow.")

        XCTAssertEqual(h.chat.entries.last?.content, .failure)
        XCTAssertTrue(h.chat.hasFailedTurn)
        XCTAssertFalse(h.chat.entries.contains { if case .agent(let text, _, _) = $0.content { text.contains("Got it: no laptop") } else { false } },
                       "streamed text from a failed turn is discarded")
        XCTAssertEqual(h.chat.transcript.map(\.role), [.user])
        XCTAssertNil(h.pending.pending, "nothing was staged")

        await h.mock.setVariant("laptop", for: "postAgentMessage")
        await h.chat.retry()
        XCTAssertFalse(h.chat.hasFailedTurn)
        XCTAssertEqual(h.chat.transcript.map(\.role), [.user, .assistant])
        let requests = try await h.transport.agentRequests()
        XCTAssertEqual(requests.count, 2, "Retry sends a second, independent request")
        XCTAssertEqual(requests[1].messages, requests[0].messages, "the retry resends the same transcript")
        XCTAssertNotNil(h.pending.pending)
    }

    func testPreStreamFailureShowsRetryRow() async throws {
        let h = try T52Harness()
        await h.mock.setVariant("last_not_user", for: "postAgentMessage")
        await h.chat.submit("hello")
        XCTAssertEqual(h.chat.entries.last?.content, .failure)
        XCTAssertFalse(h.chat.isStreaming)
    }

    func testRetryWithoutFailureDoesNothing() async throws {
        let h = try T52Harness()
        await h.chat.retry()
        let requests = try await h.transport.agentRequests()
        XCTAssertTrue(requests.isEmpty, "the LLM is never invoked without a user action")
    }

    func testOpeningMessageIsStaticAndNotSent() async throws {
        let h = try T52Harness()
        XCTAssertEqual(h.chat.entries.count, 1)
        XCTAssertEqual(h.chat.entries.first?.content, .agent(text: ChatModel.openingMessage, state: .done, status: nil))
        try await Task.sleep(for: .milliseconds(20))
        let requests = try await h.transport.agentRequests()
        XCTAssertTrue(requests.isEmpty)
        await h.chat.submit("Reading, 2 hours a week")
        let sent = try await h.transport.agentRequests()
        XCTAssertEqual(sent.first?.messages, [TranscriptMessage(role: .user, content: "Reading, 2 hours a week")])
    }

    func testTranscriptAccumulatesAcrossTurns() async throws {
        let h = try T52Harness()
        await h.chat.submit("I don't have my laptop today or tomorrow.")
        await h.mock.setVariant("refusal", for: "postAgentMessage")
        await h.chat.submit("Something else")

        let laptop = try T52Fixtures.agentDone("post_agent_message__laptop.json")
        let requests = try await h.transport.agentRequests()
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests[1].messages, [
            TranscriptMessage(role: .user, content: "I don't have my laptop today or tomorrow."),
            TranscriptMessage(role: .assistant, content: laptop.reply, changesetId: laptop.changeset?.id),
            TranscriptMessage(role: .user, content: "Something else"),
        ])
        XCTAssertEqual(h.chat.transcript.count, 4)
        let agentPosts = await h.transport.requests(method: "POST", path: "/agent/message")
        let body = try XCTUnwrap(agentPosts.last?.httpBody)
        XCTAssertTrue(String(decoding: body, as: UTF8.self).contains(laptop.changeset!.id.pathComponent), "changeset_id goes out lowercase")
    }

    func testBlankMessagesAreNotSent() async throws {
        let h = try T52Harness()
        h.chat.draft = "   \n "
        XCTAssertFalse(h.chat.canSend)
        await h.chat.submit("   ")
        XCTAssertEqual(h.chat.entries.count, 1)
    }

    func testTranscriptTrimmingHonoursContractLimits() {
        var transcript = (0..<205).map { TranscriptMessage(role: $0.isMultiple(of: 2) ? .user : .assistant, content: "m\($0)") }
        transcript[204].content = String(repeating: "x", count: 9000)
        let trimmed = ChatModel.trimmedForRequest(transcript)
        XCTAssertEqual(trimmed.count, 200)
        XCTAssertEqual(trimmed.first?.content, "m5", "oldest messages are dropped first")
        XCTAssertEqual(trimmed.last?.content.count, 8000)
    }
}
