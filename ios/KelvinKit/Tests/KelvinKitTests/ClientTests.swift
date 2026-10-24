import XCTest
@testable import KelvinKit
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Answers requests from a script, and remembers what was asked.
final class Scripted: Transport, @unchecked Sendable {
    struct Reply {
        var status: Int = 200
        var body: String
    }

    var replies: [String: Reply] = [:]        // "METHOD /path" -> reply
    var unreachable: Set<String> = []          // hosts that never answer
    private(set) var asked: [URLRequest] = []
    private let lock = NSLock()

    func on(_ key: String, _ status: Int, _ body: String) {
        replies[key] = Reply(status: status, body: body)
    }

    func on(_ key: String, _ body: String) { on(key, 200, body) }

    private func reply(_ request: URLRequest) throws -> (Data, HTTPURLResponse) {
        lock.lock()
        asked.append(request)
        lock.unlock()
        let url = request.url!
        if unreachable.contains(url.host ?? "") { throw URLError(.timedOut) }
        let key = "\(request.httpMethod ?? "GET") \(url.path)"
        let r = replies[key] ?? Reply(status: 404, body: "{\"error\": \"not found\"}")
        let response = HTTPURLResponse(url: url, statusCode: r.status, httpVersion: "HTTP/1.1", headerFields: [:])!
        return (Data(r.body.utf8), response)
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) { try reply(request) }

    func lines(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<String, Error>) {
        let (data, response) = try reply(request)
        let lines = String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init)
        return (response, AsyncThrowingStream { c in
            for l in lines { c.yield(l) }
            c.finish()
        })
    }

    func body(_ i: Int) -> JSONValue? {
        asked[i].httpBody.flatMap { try? JSONDecoder().decode(JSONValue.self, from: $0) }
    }
}

final class ClientTests: XCTestCase {
    let base = URL(string: "http://192.168.1.50:8771")!

    func testPairingSendsTheCodeAndNoToken() async throws {
        let t = Scripted()
        t.on("POST /api/pair", "{\"token\": \"tok\", \"name\": \"Ana's phone\", \"role\": \"control\"}")
        let r = try await KelvinClient(base: base, token: nil, transport: t).pair(code: "123 456")
        XCTAssertEqual(r, PairResult(token: "tok", name: "Ana's phone", role: "control"))
        XCTAssertEqual(t.body(0)?["code"], .string("123456"))
        XCTAssertNil(t.asked[0].value(forHTTPHeaderField: "X-Kelvin-Token"))
    }

    func testEveryCallCarriesTheToken() async throws {
        let t = Scripted()
        t.on("GET /api/me", "{\"role\": \"watch\", \"name\": \"Lab tablet\", \"lab\": \"Cryostat lab\"}")
        let me = try await KelvinClient(base: base, token: "tok", transport: t).me()
        XCTAssertEqual(me.role, "watch")
        XCTAssertEqual(me.addresses, [])                         // an older Kelvin says nothing about them
        XCTAssertEqual(t.asked[0].value(forHTTPHeaderField: "X-Kelvin-Token"), "tok")
        XCTAssertEqual(t.asked[0].url?.absoluteString, "http://192.168.1.50:8771/api/me")
    }

    func testKelvinsRefusalsInWords() async {
        let t = Scripted()
        t.on("POST /api/pair", 403, "{\"error\": \"That code is wrong or has expired.\"}")
        t.on("GET /api/state", 401, "{\"error\": \"Nobody is signed in to Kelvin on its computer now.\", \"signin\": true}")
        t.on("GET /api/chats", 403, "{\"error\": \"A watch device cannot do that. Use the Kelvin computer.\"}")
        t.on("GET /api/runs", 401, "{\"error\": \"This device is not paired with Kelvin, or was removed.\"}")
        let fresh = KelvinClient(base: base, token: nil, transport: t)
        let paired = KelvinClient(base: base, token: "tok", transport: t)
        await assertFails(.forbidden, "That code is wrong or has expired.") { _ = try await fresh.pair(code: "000000") }
        await assertFails(.signedOut, "Nobody is signed in to Kelvin on its computer now.") { _ = try await paired.state() }
        await assertFails(.forbidden, "A watch device cannot do that. Use the Kelvin computer.") { _ = try await paired.chats() }
        await assertFails(.unpaired, "This phone is no longer paired with Kelvin. Pair it again.") { _ = try await paired.runs() }
        t.unreachable = ["192.168.1.50"]
        await assertFails(.unreachable, "Can't reach Kelvin at 192.168.1.50: it did not answer in time.") {
            _ = try await paired.state()
        }
    }

    func testAPairingLockIsNotAWrongCode() async {
        let t = Scripted()
        t.on("POST /api/pair", 429, "{\"error\": \"Too many wrong codes. Pairing is locked for a few minutes.\"}")
        await assertFails(.locked, "Too many wrong codes. Pairing is locked for a few minutes.") {
            _ = try await KelvinClient(base: self.base, token: nil, transport: t).pair(code: "111111")
        }
    }

    func testAnswersAndStops() async throws {
        let t = Scripted()
        for key in ["POST /api/approvals/r-7", "POST /api/alarms/a1/ack", "POST /api/runs/stop", "POST /api/stop"] {
            t.on(key, "{\"ok\": true}")
        }
        let c = KelvinClient(base: base, token: "tok", transport: t)
        try await c.answer(ApprovalRequest(id: "r-7", title: "Raise the field", items: []), approve: false)
        try await c.acknowledge(alarm("a1"))
        try await c.stopRun()
        try await c.stopChat()
        XCTAssertEqual(t.body(0)?["approve"], .bool(false))
        XCTAssertEqual(t.asked.map { $0.url!.path }, ["/api/approvals/r-7", "/api/alarms/a1/ack", "/api/runs/stop", "/api/stop"])
    }

    func testHandsOnTheSample() async throws {
        let t = Scripted()
        t.on("POST /api/hands", "{\"hands_on\": {\"by\": \"human (phone Lab iPhone)\", \"since\": \"2026-10-24T14:02:11\"},"
             + " \"lines\": [\"smu.V 3 V → 0 V, 6 s (safe state)\"], \"mounted\": null}")
        let c = KelvinClient(base: base, token: "tok", transport: t)
        let r = try await c.hands(on: true)
        XCTAssertEqual(r.handsOn?.who, "Lab iPhone")
        XCTAssertEqual(r.lines, ["smu.V 3 V → 0 V, 6 s (safe state)"])
        XCTAssertEqual(t.body(0)?["on"], .bool(true))
        XCTAssertEqual(t.asked.first?.url?.path, "/api/hands")
    }

    func testFileNamesAreEncoded() async throws {
        let t = Scripted()
        t.on("GET /api/runs/20261022-101500-iv/files/report 2.pdf", "%PDF")
        let data = try await KelvinClient(base: base, token: "tok", transport: t).runFile("20261022-101500-iv", "report 2.pdf")
        XCTAssertEqual(String(decoding: data, as: UTF8.self), "%PDF")
        XCTAssertTrue(t.asked[0].url!.absoluteString.hasSuffix("/files/report%202.pdf"))
    }

    func testLiveAsksFromWhereItLeftOff() async throws {
        let t = Scripted()
        t.on("GET /api/live", "{\"live\": null}")
        let live = try await KelvinClient(base: base, token: "tok", transport: t).live(since: 12)
        XCTAssertNil(live)
        XCTAssertEqual(t.asked[0].url?.query, "since=12")
    }

    func testTheChatStream() async throws {
        let t = Scripted()
        t.on("POST /api/chats/c1/turn", [
            "{\"type\": \"turn_start\", \"model\": \"anthropic:claude-opus-5-5\", \"title\": \"IV sweep\"}",
            "{\"type\": \"text\", \"delta\": \"Sweeping \"}",
            "{\"type\": \"thinking\", \"delta\": \"hmm\"}",
            "not json",
            "{\"type\": \"text\", \"delta\": \"now.\"}",
            "{\"type\": \"turn_end\", \"reason\": \"end\"}",
        ].joined(separator: "\n"))
        let c = KelvinClient(base: base, token: "tok", transport: t)
        var events: [ChatEvent] = []
        for try await e in try await c.turn(chat: "c1", text: "sweep", model: "anthropic:claude-opus-5-5") {
            events.append(e)
        }
        XCTAssertEqual(events, [.started(model: "anthropic:claude-opus-5-5", title: "IV sweep"), .text("Sweeping "),
                                .other("thinking"), .text("now."), .ended(reason: "end")])
        XCTAssertEqual(t.body(0)?["text"], .string("sweep"))
        XCTAssertEqual(t.asked[0].timeoutInterval, 3600)        // an answer may wait long for an approval
    }

    func testABusyChatSaysSo() async {
        let t = Scripted()
        t.on("POST /api/chats/c1/turn", 409, "{\"error\": \"Another conversation is still running.\"}")
        await assertFails(.conflict, "Another conversation is still running.") {
            _ = try await KelvinClient(base: self.base, token: "tok", transport: t).turn(chat: "c1", text: "hi", model: "m")
        }
    }

    func testReachTriesTheNextAddressOnlyWhenOneDoesNotAnswer() async throws {
        let t = Scripted()
        t.on("GET /api/me", "{\"role\": \"control\"}")
        t.unreachable = ["192.168.1.50"]
        let tail = URL(string: "http://100.101.7.9:8771")!
        let (client, me) = try await reach([base, tail], token: "tok", transport: t) { try await $0.me() }
        XCTAssertEqual(client.base, tail)
        XCTAssertEqual(me.role, "control")

        let refusing = Scripted()
        refusing.on("GET /api/me", 401, "{\"error\": \"not paired\"}")
        do {
            _ = try await reach([base, tail], token: "tok", transport: refusing) { try await $0.me() }
            XCTFail("a refusal is an answer")
        } catch let e as KelvinError {
            XCTAssertEqual(e.kind, .unpaired)
            XCTAssertEqual(refusing.asked.count, 1)
        }
    }

    // ------------------------------------------------------------------ helpers

    func alarm(_ id: String) -> Alarm {
        try! JSONDecoder().decode(Alarm.self, from: Data("{\"id\": \"\(id)\", \"text\": \"He level low\"}".utf8))
    }

    func assertFails(_ kind: KelvinError.Kind, _ message: String, file: StaticString = #filePath, line: UInt = #line,
                     _ work: () async throws -> Void) async {
        do {
            try await work()
            XCTFail("expected \(kind)", file: file, line: line)
        } catch let e as KelvinError {
            XCTAssertEqual(e.kind, kind, file: file, line: line)
            XCTAssertEqual(e.message, message, file: file, line: line)
        } catch {
            XCTFail("unexpected \(error)", file: file, line: line)
        }
    }
}
