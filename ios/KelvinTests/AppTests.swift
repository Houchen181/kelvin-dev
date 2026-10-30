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

    func testTheKelvinAccountReachesItsKelvinsThroughTheHub() async throws {
        let script = Script()
        script.on("POST /api/signin", "{\"access_token\": \"khub_phone\", \"user\": {\"name\": \"Ada\", \"email\": \"ada@example.org\"}}")
        script.on("GET /api/relay/kelvins", "{\"kelvins\": [{\"id\": \"lab-pc-1\", \"name\": \"LAB-PC\", \"platform\": \"win32\", \"online\": true}]}")
        script.on("GET /api/relay/kelvins/lab-pc-1/api/me", "{\"role\": \"control\", \"name\": \"iPhone\", \"lab\": \"Cryostat lab\", \"computer\": \"LAB-PC\"}")
        script.on("POST /api/relay/kelvins/lab-pc-1/api/phone/direct", "{\"available\": true, \"token\": \"direct-token\","
                  + " \"addresses\": [\"http://192.168.1.50:8771\"], \"routes\": [{\"url\": \"http://192.168.1.50:8771\", \"kind\": \"network\"}]}")
        script.on("GET /api/relay/kelvins/lab-pc-1/api/state", "{\"lab\": \"Cryostat lab\"}")
        script.unreachable = ["192.168.1.50"]                                  // away from the lab
        let app = AppModel(transport: script, defaults: defaults, tokens: tokens)
        try await app.signIn(hub: URL(string: "https://kelvin.example.org")!, email: "ada@example.org", password: "pw")
        let c = try XCTUnwrap(app.current)
        XCTAssertTrue(c.viaAccount)
        XCTAssertEqual(tokens.token(for: Account.tokenID), "khub_phone")
        let kept = String(decoding: defaults.data(forKey: "kelvin.account") ?? Data(), as: UTF8.self)
        XCTAssertTrue(kept.contains("ada@example.org") && !kept.contains("khub_phone"))
        let session = app.session(for: c)
        await session.refresh()
        XCTAssertNil(session.problem)
        XCTAssertEqual(session.route, .hub)
        XCTAssertEqual(session.state?.lab, "Cryostat lab")
        XCTAssertEqual(tokens.token(for: c.id), "direct-token")               // its own token, for the lab network
        XCTAssertEqual(app.current?.addresses, [lan])

        script.unreachable = []                                              // back in the lab: directly, no hub
        script.on("GET /api/me", me)
        script.on("GET /api/state", "{\"lab\": \"Cryostat lab\"}")
        let later = AppModel(transport: script, defaults: defaults, tokens: tokens)
        let direct = later.session(for: try XCTUnwrap(later.current))
        await direct.refresh()
        XCTAssertEqual(direct.connected, lan)

        script.on("DELETE /api/relay/kelvins/lab-pc-1/api/phone/direct", "{\"ok\": true}")
        script.on("POST /api/signout", 204, "")
        await app.signOut()
        XCTAssertNil(app.account)
        XCTAssertNil(app.current)
        XCTAssertNil(tokens.token(for: Account.tokenID))
        XCTAssertNil(tokens.token(for: c.id))
    }

    func testTheTeamChatOnThePhone() async throws {
        let script = Script()
        let (app, c) = try await paired(script)
        script.on("GET /api/state", "{\"lab\": \"Cryostat lab\", \"team\": 3}")
        let session = app.session(for: c)
        await session.refresh()
        XCTAssertEqual(session.teamUnread, 0)                                 // what was there before is not news
        script.on("GET /api/state", "{\"lab\": \"Cryostat lab\", \"team\": 5}")
        await session.refresh()
        XCTAssertEqual(session.teamUnread, 2)
        script.on("GET /api/team", "{\"messages\": [{\"id\": 4, \"at\": \"2026-10-30T09:12:44-04:00\", \"name\": \"Ben\","
                  + " \"text\": \"Fridge at base\"}, {\"id\": 5, \"name\": \"Ben\", \"text\": \"On D3 until 6\"}], \"last\": 5}")
        let team = TeamModel(session: session)
        await team.load()
        XCTAssertEqual(team.thread.messages.map(\.id), [4, 5])
        session.readTeam(upTo: team.thread.last)
        XCTAssertEqual(session.teamUnread, 0)
        script.on("POST /api/team", "{\"message\": {\"id\": 6, \"name\": \"Ana\", \"via\": \"phone Ana's phone\","
                  + " \"text\": \"Taking the magnet\"}, \"last\": 6}")
        team.draft = "Taking the magnet"
        let sent = await team.send(as: "Ana")
        XCTAssertTrue(sent)
        XCTAssertEqual(team.thread.last, 6)
        XCTAssertEqual(team.draft, "")
        XCTAssertEqual(session.teamRead, 6)                                   // its own message is read
        app.teamName = "Ana"
        let later = AppModel(transport: script, defaults: defaults, tokens: tokens)
        XCTAssertEqual(later.teamRead(c), 6)                                  // kept on the phone
        XCTAssertEqual(later.teamName, "Ana")
    }

    func testTheDemoLabHasATeamChat() async throws {
        let c = KelvinClient(base: DemoTransport.address, token: "demo", transport: DemoTransport.shared)
        let first = try await c.team()
        XCTAssertFalse(first.messages.isEmpty)
        let sent = try await c.postTeam(name: "Tester", text: "Hello from the tests")
        XCTAssertEqual(sent.id, first.last + 1)
        let now = try await c.state()
        XCTAssertEqual(now.team, sent.id)
        let after = try await c.team(after: first.last)
        XCTAssertEqual(after.messages.map(\.text), ["Hello from the tests"])
    }

    func testTheDemoLabWorksOutAHallSlope() async throws {
        let c = KelvinClient(base: DemoTransport.address, token: "demo", transport: DemoTransport.shared)
        let cols = try await c.analysisColumns(run: DemoTransport.fieldRunID)
        XCTAssertEqual(cols.readings.map(\.key), ["lockin.x", "lockin.y"])
        XCTAssertEqual(cols.defaultX, "magnet.field")
        var spec = AnalysisSpec(runs: [DemoTransport.fieldRunID], x: "magnet.field", y: "lockin.y",
                                steps: [AnalysisStep(.antisymmetrize)], fit: AnalysisFit(model: "linear"))
        let r = try await c.analyze(spec)
        XCTAssertEqual(r.y.key, "lockin.y (antisymmetric part)")
        let slope = try XCTUnwrap(r.curves.first?.fit?.params.first { $0.name == "b" }?.value)
        XCTAssertEqual(slope, 4.0e-7, accuracy: 2e-9)                         // the pickup of the resistance is gone
        let (name, data) = try await c.analysisCSV(spec)
        XCTAssertEqual(name, DemoTransport.fieldRunID + "-analysis.csv")
        XCTAssertTrue(String(decoding: data, as: UTF8.self).hasPrefix("magnet.field [T]"))
        let kept = try await c.keepFit(spec, curve: 0)
        guard case .kept(let said) = kept else { return XCTFail("not kept") }
        XCTAssertEqual(KeepAnswer.name(said), "lockin.y linear")
        spec.fit = AnalysisFit(model: "lorentzian")
        let other = try await c.analyze(spec)
        XCTAssertNotNil(other.curves.first?.fit?.error)                       // the demo fits only a line
    }

    func testAnalysisOnThePhone() async throws {
        let app = AppModel(transport: Script(), defaults: defaults, tokens: tokens)
        app.startDemo()
        let session = app.session(for: try XCTUnwrap(app.current))
        let model = AnalysisModel(session: session, run: DemoTransport.fieldRunID)
        await model.start()
        XCTAssertEqual(model.spec.x, "magnet.field")                         // x and y follow the run
        XCTAssertEqual(model.spec.y, "lockin.x")
        XCTAssertEqual(model.result?.curves.count, 1)
        XCTAssertEqual(model.drawn?.y.unit, "µV")
        model.setY("lockin.y")
        model.addStep(.antisymmetrize)
        model.setFit("linear", formula: false)
        await model.compute(now: true).value
        XCTAssertEqual(model.answered?.fit?.model, "linear")
        XCTAssertEqual(model.result?.curves.first?.fit?.params.count, 2)
        XCTAssertFalse(model.working)
        model.addStep(.fft)                                                   // the demo lab can't: it says so
        await model.compute(now: true).value
        XCTAssertNotNil(model.problem)
        XCTAssertEqual(model.answered?.steps.count, 1)                        // the last plot it could draw stays
        if case .kept = try await model.keep(0) {} else { XCTFail("not kept") }
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
