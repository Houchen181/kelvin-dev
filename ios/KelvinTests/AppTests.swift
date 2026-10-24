import KelvinKit
import Security
import XCTest
@testable import Kelvin

/// Answers requests from a script (no network), and remembers what was asked.
final class Script: Transport, @unchecked Sendable {
    var replies: [String: (Int, String)] = [:]
    var unreachable: Set<String> = []
    private(set) var asked: [String] = []
    private let lock = NSLock()

    func on(_ key: String, _ status: Int, _ body: String) { replies[key] = (status, body) }
    func on(_ key: String, _ body: String) { on(key, 200, body) }

    private func reply(_ request: URLRequest) throws -> (Data, HTTPURLResponse) {
        let url = request.url!
        let key = "\(request.httpMethod ?? "GET") \(url.path)"
        lock.lock()
        asked.append("\(url.host ?? "") \(key)")
        lock.unlock()
        if unreachable.contains(url.host ?? "") { throw URLError(.timedOut) }
        let (status, body) = replies[key] ?? (404, "{\"error\": \"not found\"}")
        return (Data(body.utf8), HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) { try reply(request) }

    func lines(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<String, Error>) {
        let (data, response) = try reply(request)
        let lines = String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init)
        return (response, AsyncThrowingStream { c in
            lines.forEach { c.yield($0) }
            c.finish()
        })
    }
}

final class Tokens: TokenStore {
    var kept: [UUID: String] = [:]
    func save(_ token: String, for id: UUID) { kept[id] = token }
    func token(for id: UUID) -> String? { kept[id] }
    func delete(_ id: UUID) { kept[id] = nil }
}

@MainActor
final class AppTests: XCTestCase {
    let lan = URL(string: "http://192.168.1.50:8771")!
    let tail = URL(string: "http://100.101.7.9:8771")!
    lazy var defaults = UserDefaults(suiteName: "kelvin-tests-\(UUID().uuidString)")!
    let tokens = Tokens()

    let me = """
    {"role": "control", "name": "Ana's phone", "lab": "Cryostat lab", "computer": "LAB-PC", "version": "0.3.15", "api": 1,
     "addresses": ["http://192.168.1.50:8771", "http://100.101.7.9:8771"]}
    """

    func paired(_ script: Script) async throws -> (AppModel, Computer) {
        script.on("POST /api/pair", "{\"token\": \"secret-token\", \"name\": \"Ana's phone\", \"role\": \"control\"}")
        script.on("GET /api/me", me)
        let app = AppModel(transport: script, defaults: defaults, tokens: tokens)
        return (app, try await app.pair(at: lan, code: "123456"))
    }

    func testPairingKeepsTheTokenOutOfThePlainSettings() async throws {
        let (app, c) = try await paired(Script())
        XCTAssertEqual(tokens.token(for: c.id), "secret-token")
        XCTAssertEqual(c.lab, "Cryostat lab")
        XCTAssertEqual(c.addresses, [lan, tail])
        XCTAssertEqual(app.current?.id, c.id)
        let kept = String(decoding: defaults.data(forKey: "kelvin.computers") ?? Data(), as: UTF8.self)
        XCTAssertTrue(kept.contains("LAB-PC"))
        XCTAssertFalse(kept.contains("secret-token"))                          // never in the plain settings
        XCTAssertEqual(AppModel(transport: Script(), defaults: defaults, tokens: tokens).current?.id, c.id)   // kept
    }

    func testPairingTriesEveryAddressInTheCode() async throws {
        let script = Script()
        script.on("POST /api/pair", "{\"token\": \"secret-token\", \"name\": \"Ana's phone\", \"role\": \"control\"}")
        script.on("GET /api/me", me)
        script.unreachable = ["192.168.1.50"]                                 // not on the lab's Wi-Fi
        let app = AppModel(transport: script, defaults: defaults, tokens: tokens)
        let link = try XCTUnwrap(Pairing.parse("http://192.168.1.50:8771/pair#123456&also=100.101.7.9:8771"))
        let c = try await app.pair(at: link.addresses, code: try XCTUnwrap(link.code))
        XCTAssertEqual(c.addresses.first, tail)
        XCTAssertEqual(tokens.token(for: c.id), "secret-token")
        XCTAssertEqual(script.asked.filter { $0.hasSuffix("POST /api/pair") }.count, 2)   // reached Kelvin only once
    }

    func testAWrongCodeSaysSo() async {
        let script = Script()
        script.on("POST /api/pair", 403, "{\"error\": \"That code is wrong or has expired.\"}")
        let app = AppModel(transport: script, defaults: defaults, tokens: tokens)
        do {
            try await app.pair(at: lan, code: "000000")
            XCTFail("paired with a wrong code")
        } catch {
            XCTAssertEqual(error.localizedDescription, "That code is wrong or has expired.")
        }
        XCTAssertNil(app.current)
    }

    func testAwayFromTheLabItGoesThroughTailscale() async throws {
        let script = Script()
        let (app, c) = try await paired(script)
        script.on("GET /api/state", "{\"lab\": \"Cryostat lab\", \"pending\": [{\"id\": \"r1\", \"title\": \"Raise the field\", \"items\": []}]}")
        script.unreachable = ["192.168.1.50"]
        let session = app.session(for: c)
        await session.refresh()
        XCTAssertNil(session.problem)
        XCTAssertEqual(session.connected, tail)
        XCTAssertEqual(session.state?.pending.first?.title, "Raise the field")
        XCTAssertEqual(app.current?.addresses.first, tail)                      // tried first next time

        script.unreachable = ["192.168.1.50", "100.101.7.9"]
        await session.refresh()
        XCTAssertEqual(session.problem?.kind, .unreachable)
        XCTAssertNil(session.connected)
        XCTAssertEqual(session.state?.lab, "Cryostat lab")                      // the last state stays on screen
    }

    func testUnpairedOnTheComputer() async throws {
        let script = Script()
        let (app, c) = try await paired(script)
        script.on("GET /api/me", 401, "{\"error\": \"This device is not paired with Kelvin, or was removed.\"}")
        let session = app.session(for: c)
        await session.refresh()
        XCTAssertEqual(session.problem?.kind, .unpaired)
    }

    func testAChatFromThePhone() async throws {
        let script = Script()
        let (app, c) = try await paired(script)
        script.on("GET /api/bootstrap", "{\"models\": {\"groups\": [{\"models\": [{\"key\": \"anthropic:claude-opus-5-5\", \"label\": \"Claude Opus 5.5\"}]}]}, \"default_model\": \"anthropic:claude-opus-5-5\"}")
        script.on("POST /api/chats", "{\"id\": \"c9\", \"title\": \"Incognito chat\", \"model\": \"anthropic:claude-opus-5-5\", \"incognito\": true, \"messages\": []}")
        script.on("POST /api/chats/c9/turn", [
            "{\"type\": \"turn_start\", \"model\": \"anthropic:claude-opus-5-5\", \"title\": \"Incognito chat\"}",
            "{\"type\": \"text\", \"delta\": \"1 bar is 100 kPa.\"}",
            "{\"type\": \"turn_end\", \"reason\": \"end\"}",
        ].joined(separator: "\n"))
        let chat = ChatModel(session: app.session(for: c), chat: nil, incognito: true)
        chat.draft = "How many kPa is a bar?"
        await chat.send()?.value
        XCTAssertEqual(chat.transcript.lines.map(\.text), ["How many kPa is a bar?", "1 bar is 100 kPa."])
        XCTAssertFalse(chat.transcript.answering)
        XCTAssertEqual(chat.draft, "")
        XCTAssertTrue(script.asked.contains("192.168.1.50 POST /api/chats"))
    }

    func testTheKeychain() throws {
        let keychain = Keychain(service: "Kelvin tests")
        let id = UUID()
        let status = keychain.store("secret-token", for: id)
        if status == errSecMissingEntitlement { throw XCTSkip("no Keychain in an unsigned simulator build") }
        XCTAssertEqual(status, errSecSuccess)
        XCTAssertEqual(keychain.token(for: id), "secret-token")
        keychain.save("newer", for: id)
        XCTAssertEqual(keychain.token(for: id), "newer")
        keychain.delete(id)
        XCTAssertNil(keychain.token(for: id))
    }

    func testNotificationsOnlyForWhatIsNew() throws {
        let c = Computer(lab: "Cryostat lab", phoneName: "P", role: "control", addresses: [lan])
        func state(_ asks: [String]) throws -> LabState {
            let pending = asks.map { "{\"id\": \"\($0)\", \"title\": \"Ask \($0)\", \"items\": []}" }.joined(separator: ",")
            return try JSONDecoder().decode(LabState.self, from: Data("{\"lab\": \"L\", \"pending\": [\(pending)]}".utf8))
        }
        let n = Notifier()
        XCTAssertEqual(n.look(at: try state(["r1"]), on: c), [])                // on screen already
        XCTAssertEqual(n.look(at: try state(["r1", "r2"]), on: c), ["Kelvin asks: Ask r2"])
        XCTAssertEqual(n.look(at: try state(["r2"]), on: c), [])
    }
}
