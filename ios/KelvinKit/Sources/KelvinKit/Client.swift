import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// How requests reach Kelvin. The app uses URLSession; tests use a script.
public protocol Transport: Sendable {
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse)
    /// The response's body line by line, as it arrives (the chat stream is one JSON event per line).
    func lines(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<String, Error>)
}

/// What went wrong, in words for the screen, and what kind of thing it was.
public struct KelvinError: LocalizedError, Equatable, Sendable {
    public enum Kind: Equatable, Sendable {
        case unpaired      // the token is gone: pair again
        case signedOut     // still paired, but its person isn't signed in on the Kelvin computer
        case forbidden     // this phone's role can't do that
        case locked        // too many wrong pairing codes
        case conflict      // already answered, already running...
        case server
        case unreachable
        case unreadable
    }

    public var kind: Kind
    public var message: String

    public init(_ kind: Kind, _ message: String) {
        self.kind = kind
        self.message = message
    }

    public var errorDescription: String? { message }

    /// Kelvin's answer to a request that failed: {"error": "...", "signin": true}. account: it came through the
    /// Kelvin hub, which says itself why (signed out of the account, the Kelvin offline).
    static func from(status: Int, body: Data, paired: Bool, account: Bool = false) -> KelvinError {
        let json = try? JSONDecoder().decode(JSONValue.self, from: body)
        let said = json?["error"]?.string
        switch status {
        case 401 where json?["signin"]?.bool == true:
            return KelvinError(.signedOut, said ?? "Nobody is signed in to Kelvin on its computer now.")
        case 401 where account:
            return KelvinError(.unpaired, said ?? "Sign in to your Kelvin account again.")
        case 503 where json?["offline"]?.bool == true:
            return KelvinError(.unreachable, said ?? "That Kelvin is offline.")
        case 401:
            return KelvinError(.unpaired, paired ? "This phone is no longer paired with Kelvin. Pair it again."
                                                 : (said ?? "Kelvin did not accept this phone."))
        case 403:
            return KelvinError(.forbidden, said ?? "This phone cannot do that. Use the Kelvin computer.")
        case 409, 423:
            return KelvinError(.conflict, said ?? "Kelvin is busy with that already.")
        case 429:
            return KelvinError(.locked, said ?? "Too many wrong codes. Wait a few minutes and make a new code.")
        default:
            return KelvinError(.server, said ?? "Kelvin answered HTTP \(status).")
        }
    }

    static func unreachable(_ base: URL, _ error: Error) -> KelvinError {
        let host = base.host ?? base.absoluteString
        let why = (error as? URLError).map { e -> String in
            switch e.code {
            case .timedOut: return "it did not answer in time"
            case .cannotConnectToHost: return "nothing answered there (is phone access on?)"
            case .notConnectedToInternet: return "this phone is offline"
            case .cannotFindHost, .dnsLookupFailed: return "that name is unknown here (is Tailscale on?)"
            default:   // -1022: App Transport Security
                return e.code.rawValue == -1022 ? "iOS refused a plain connection to it" : e.localizedDescription
            }
        } ?? error.localizedDescription
        return KelvinError(.unreachable, "Can't reach Kelvin at \(host): \(why).")
    }
}

/// Kelvin's phone API at one address. Every call is one request; nothing is cached.
public struct KelvinClient: Sendable {
    public var base: URL
    public var token: String?
    public var transport: Transport
    public var timeout: TimeInterval
    /// Through the Kelvin hub: base is the hub's way to one Kelvin (HubClient.relayBase), and token the phone's
    /// token on the hub, sent as a bearer token.
    public var bearer: Bool

    public init(base: URL, token: String?, transport: Transport, timeout: TimeInterval = 10, bearer: Bool = false) {
        self.base = base
        self.token = token
        self.transport = transport
        self.timeout = timeout
        self.bearer = bearer
    }

    // ------------------------------------------------------------------ the calls

    /// Offer a pairing code; Kelvin answers with this phone's own token.
    public func pair(code: String) async throws -> PairResult {
        try await call("POST", "/api/pair", body: .object(["code": .string(Pairing.digits(code))]))
    }

    public func me() async throws -> Me { try await call("GET", "/api/me") }

    public func state() async throws -> LabState { try await call("GET", "/api/state") }

    public func live(since: Int) async throws -> LiveRun? {
        let answer: LiveAnswer = try await call("GET", "/api/live", query: ["since": String(since)])
        return answer.live
    }

    public func runs() async throws -> [RunSummary] {
        let answer: RunsAnswer = try await call("GET", "/api/runs")
        return answer.runs
    }

    public func run(_ id: String) async throws -> RunDetail { try await call("GET", "/api/runs/" + segment(id)) }

    /// A run's report or data file, to open on the phone.
    public func runFile(_ id: String, _ name: String) async throws -> Data {
        let request = self.request("GET", "/api/runs/\(segment(id))/files/\(segment(name))", timeout: 60)
        return try await send(request)
    }

    public func answer(_ request: ApprovalRequest, approve: Bool) async throws {
        let _: JSONValue = try await call("POST", "/api/approvals/" + segment(request.id),
                                          body: .object(["approve": .bool(approve)]))
    }

    public func acknowledge(_ alarm: Alarm) async throws {
        let _: JSONValue = try await call("POST", "/api/alarms/\(segment(alarm.id))/ack", body: .object([:]))
    }

    /// Stop the run in progress (its safe-stop steps run).
    public func stopRun() async throws {
        let _: JSONValue = try await call("POST", "/api/runs/stop", body: .object([:]))
    }

    /// Hands on the sample (Kelvin 0.3.17 and later): Kelvin stops what runs and holds everything that reaches the
    /// sample at safe until a person unlocks. Any paired phone can lock; unlocking needs one that can approve.
    @discardableResult
    public func hands(on: Bool) async throws -> HandsResult {
        try await call("POST", "/api/hands", body: .object(["on": .bool(on)]))
    }

    /// Through the hub only: a token of this phone's own for reaching that Kelvin directly (on the lab network or
    /// through Tailscale), when its phone access is on.
    public func direct() async throws -> DirectAccess { try await call("POST", "/api/phone/direct", body: .object([:])) }

    /// Through the hub only: the Kelvin forgets the token direct() gave (the phone signed out of the account).
    public func forgetDirect() async throws {
        let _: JSONValue = try await call("DELETE", "/api/phone/direct")
    }

    /// Stop the conversation Kelvin is answering, and decline what it asked.
    public func stopChat() async throws {
        let _: JSONValue = try await call("POST", "/api/stop", body: .object([:]))
    }

    /// Unpair this phone on the Kelvin computer.
    public func forget() async throws {
        let _: JSONValue = try await call("POST", "/api/me/forget", body: .object([:]))
    }

    public func models() async throws -> Models { try await call("GET", "/api/bootstrap") }

    public func chats() async throws -> [ChatSummary] { try await call("GET", "/api/chats") }

    public func chat(_ id: String) async throws -> ChatDetail { try await call("GET", "/api/chats/" + segment(id)) }

    public func newChat(model: String, incognito: Bool) async throws -> ChatSummary {
        try await call("POST", "/api/chats", body: .object(["model": .string(model), "incognito": .bool(incognito)]))
    }

    /// Send a message; Kelvin's answer arrives as events while it works. The turn goes on on the computer even if
    /// this phone goes away, so a lost stream loses nothing: load the chat again.
    public func turn(chat id: String, text: String, model: String) async throws -> AsyncThrowingStream<ChatEvent, Error> {
        let body = JSONValue.object(["text": .string(text), "model": .string(model)])
        let request = self.request("POST", "/api/chats/\(segment(id))/turn", body: body, timeout: 3600)
        let (response, lines): (HTTPURLResponse, AsyncThrowingStream<String, Error>)
        do {
            (response, lines) = try await transport.lines(for: request)
        } catch {
            throw KelvinError.unreachable(base, error)
        }
        guard (200..<300).contains(response.statusCode) else {
            var body = ""
            for try await line in lines { body += line }
            throw KelvinError.from(status: response.statusCode, body: Data(body.utf8), paired: token != nil,
                                   account: bearer)
        }
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await line in lines {
                        if let event = ChatEvent(line: line) { continuation.yield(event) }
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: KelvinError.unreachable(base, error))
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // ------------------------------------------------------------------ plumbing

    func call<T: Decodable>(_ method: String, _ path: String, query: [String: String] = [:],
                            body: JSONValue? = nil) async throws -> T {
        let data = try await send(request(method, path, query: query, body: body))
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw KelvinError(.unreadable, "Kelvin's answer to \(path) was not what this app expects. Update the app "
                                         + "or Kelvin.")
        }
    }

    func send(_ request: URLRequest) async throws -> Data {
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.data(for: request)
        } catch {
            throw KelvinError.unreachable(base, error)
        }
        guard (200..<300).contains(response.statusCode) else {
            throw KelvinError.from(status: response.statusCode, body: data, paired: token != nil, account: bearer)
        }
        return data
    }

    func request(_ method: String, _ path: String, query: [String: String] = [:], body: JSONValue? = nil,
                 timeout: TimeInterval? = nil) -> URLRequest {
        var c = URLComponents(url: base, resolvingAgainstBaseURL: false) ?? URLComponents()
        var prefix = c.percentEncodedPath   // through the hub, Kelvin's routes follow the hub's path to it
        if prefix.hasSuffix("/") { prefix.removeLast() }
        c.percentEncodedPath = prefix + path
        c.queryItems = query.isEmpty ? nil : query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        var r = URLRequest(url: c.url ?? base, cachePolicy: .reloadIgnoringLocalCacheData,
                           timeoutInterval: timeout ?? self.timeout)
        r.httpMethod = method
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token {
            if bearer {
                r.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
            } else {
                r.setValue(token, forHTTPHeaderField: "X-Kelvin-Token")
            }
        }
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try? JSONEncoder().encode(body)
        }
        return r
    }

    /// One path segment, percent-encoded (ids are plain, but a file name may not be).
    func segment(_ s: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
    }
}

/// Try a computer's addresses in turn (the one that last worked first) until one answers. Only "can't reach it"
/// moves on to the next address; any answer from Kelvin, even a refusal, is the answer.
public func reach<T>(_ addresses: [URL], token: String?, transport: Transport, timeout: TimeInterval = 5,
                     _ call: (KelvinClient) async throws -> T) async throws -> (KelvinClient, T) {
    var last: Error = KelvinError(.unreachable, "This computer has no address to try.")
    for base in addresses {
        let client = KelvinClient(base: base, token: token, transport: transport, timeout: timeout)
        do {
            return (client, try await call(client))
        } catch let e as KelvinError where e.kind == .unreachable {
            last = e
        }
    }
    throw last
}
