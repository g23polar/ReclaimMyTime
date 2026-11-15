import XCTest
@testable import ReclaimMyTime

final class SSEParserTests: XCTestCase {
    private let stream = """
    : ping

    event: delta
    data: {"text":"Got it: no laptop — café"}

    event: status
    data: {"phase":"staging"}

    : heartbeat
    event: done
    data: {"outcome":"refused","reply":"Sorry.","changeset":null}


    """

    private var expected: [SSEFrame] {
        [
            SSEFrame(event: "delta", data: #"{"text":"Got it: no laptop — café"}"#),
            SSEFrame(event: "status", data: #"{"phase":"staging"}"#),
            SSEFrame(event: "done", data: #"{"outcome":"refused","reply":"Sorry.","changeset":null}"#),
        ]
    }

    func testParsesWholeStream() {
        var parser = SSEParser()
        XCTAssertEqual(parser.feed(Data(stream.utf8)), expected)
    }

    func testSplitChunkBoundaryInsideFieldAndMultibyteCharacter() throws {
        let bytes = Array(stream.utf8)
        // Split inside the 3-byte "—" (U+2014) and again inside "event:".
        let dash = try XCTUnwrap(bytes.firstIndex(of: 0xE2))
        let secondSplit = try XCTUnwrap(stream.range(of: "event: status")).lowerBound.utf16Offset(in: stream) + 3
        var parser = SSEParser()
        var frames = parser.feed(bytes[..<(dash + 1)])
        XCTAssertTrue(frames.isEmpty)
        frames += parser.feed(bytes[(dash + 1)..<secondSplit])
        frames += parser.feed(bytes[secondSplit...])
        XCTAssertEqual(frames, expected)
    }

    func testEveryPossibleSingleSplitYieldsSameFrames() {
        let bytes = Array(stream.utf8)
        for split in 0...bytes.count {
            var parser = SSEParser()
            let frames = parser.feed(bytes[..<split]) + parser.feed(bytes[split...])
            XCTAssertEqual(frames, expected, "split at \(split)")
        }
    }

    func testByteAtATime() {
        var parser = SSEParser()
        var frames: [SSEFrame] = []
        for byte in stream.utf8 { frames += parser.feed([byte]) }
        XCTAssertEqual(frames, expected)
    }

    func testCRLFAndCRLineEndings() {
        var parser = SSEParser()
        let crlf = "event: delta\r\ndata: {\"text\":\"a\"}\r\n\r\n"
        let cr = "event: delta\rdata: {\"text\":\"b\"}\r\r"
        XCTAssertEqual(parser.feed(Data(crlf.utf8)), [SSEFrame(event: "delta", data: #"{"text":"a"}"#)])
        XCTAssertEqual(parser.feed(Data(cr.utf8)), [SSEFrame(event: "delta", data: #"{"text":"b"}"#)])
    }

    func testCRLFSplitBetweenChunks() {
        var parser = SSEParser()
        var frames = parser.feed(Data("event: delta\r".utf8))
        frames += parser.feed(Data("\ndata: x\r".utf8))
        frames += parser.feed(Data("\n\r".utf8))
        frames += parser.feed(Data("\n".utf8))
        XCTAssertEqual(frames, [SSEFrame(event: "delta", data: "x")])
    }

    func testMultiLineDataAndNoSpaceAfterColon() {
        var parser = SSEParser()
        let frames = parser.feed(Data("event:status\ndata:line1\ndata: line2\n\n".utf8))
        XCTAssertEqual(frames, [SSEFrame(event: "status", data: "line1\nline2")])
    }

    func testCommentOnlyAndEventWithoutDataDispatchNothing() {
        var parser = SSEParser()
        XCTAssertEqual(parser.feed(Data(": ping\n\nevent: delta\n\n".utf8)), [])
    }

    func testUnterminatedEventIsDiscardedAtFinish() {
        var parser = SSEParser()
        XCTAssertEqual(parser.feed(Data("event: delta\ndata: {}".utf8)), [])
        parser.finish()
        XCTAssertEqual(parser.feed(Data("\n\n".utf8)), [])
    }

    func testFramesDecodeIntoAgentEvents() throws {
        var parser = SSEParser()
        let decoder = JSONCoding.makeDecoder()
        let events = try parser.feed(Data(stream.utf8)).compactMap {
            try AgentEvent.decode(eventName: $0.event, data: Data($0.data.utf8), decoder: decoder)
        }
        XCTAssertEqual(events, [
            .delta(text: "Got it: no laptop — café"),
            .status(phase: .staging),
            .done(AgentDone(outcome: .refused, reply: "Sorry.", changeset: nil)),
        ])
        XCTAssertEqual(events.map(\.isTerminal), [false, false, true])
    }

    func testUnknownEventNameIsIgnored() throws {
        XCTAssertNil(try AgentEvent.decode(eventName: "message", data: Data("{}".utf8), decoder: JSONCoding.makeDecoder()))
    }
}
