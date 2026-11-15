import Foundation

/// One dispatched server-sent event.
struct SSEFrame: Hashable, Sendable {
    let event: String
    let data: String
}

/// Incremental `text/event-stream` parser (WHATWG SSE subset used by the contract): handles
/// `event:`/`data:` fields, multi-line data, `:` comment heartbeats, LF/CRLF/CR line endings, and
/// chunk boundaries anywhere (lines are decoded only once complete, so split UTF-8 is safe).
/// `id:`/`retry:` are ignored: the contract has no resume (D13).
struct SSEParser: Sendable {
    private var lineBuffer: [UInt8] = []
    private var lastWasCR = false
    private var eventName = ""
    private var dataLines: [String] = []
    private var hasData = false

    init() {}

    /// Feeds bytes; returns every frame completed by them, in order.
    mutating func feed<Bytes: Sequence>(_ bytes: Bytes) -> [SSEFrame] where Bytes.Element == UInt8 {
        var frames: [SSEFrame] = []
        for byte in bytes {
            switch byte {
            case UInt8(ascii: "\n"):
                if lastWasCR {
                    lastWasCR = false // second half of CRLF; the line was already processed
                } else {
                    processLine(into: &frames)
                }
            case UInt8(ascii: "\r"):
                processLine(into: &frames)
                lastWasCR = true
            default:
                lastWasCR = false
                lineBuffer.append(byte)
            }
        }
        return frames
    }

    /// End of stream. Per the SSE spec an unterminated event is discarded.
    mutating func finish() {
        lineBuffer.removeAll()
        resetEvent()
    }

    private mutating func processLine(into frames: inout [SSEFrame]) {
        let line = String(decoding: lineBuffer, as: UTF8.self)
        lineBuffer.removeAll(keepingCapacity: true)

        if line.isEmpty {
            if hasData {
                frames.append(SSEFrame(event: eventName.isEmpty ? "message" : eventName, data: dataLines.joined(separator: "\n")))
            }
            resetEvent()
            return
        }
        if line.hasPrefix(":") { return } // comment / heartbeat

        let field: Substring
        var value: Substring
        if let colon = line.firstIndex(of: ":") {
            field = line[..<colon]
            value = line[line.index(after: colon)...]
            if value.hasPrefix(" ") { value = value.dropFirst() }
        } else {
            field = Substring(line)
            value = ""
        }
        switch field {
        case "event": eventName = String(value)
        case "data":
            dataLines.append(String(value))
            hasData = true
        default: break
        }
    }

    private mutating func resetEvent() {
        eventName = ""
        dataLines.removeAll()
        hasData = false
    }
}
