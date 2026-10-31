import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// A Kelvin account on the Kelvin hub (hub/relay.py, Kelvin 0.3.19 and later): the same email and password as on the
// lab's Kelvin computers. Signed in, the phone reaches every Kelvin where that account is signed in, through the hub,
// and directly too, on the lab network or through Tailscale, once a Kelvin gives it a token for that.

/// The account this phone is signed in to. Nothing secret: the hub's token for this phone is in the Keychain.
public struct Account: Codable, Equatable, Sendable {
    public var hub: URL
    public var email: String
    public var name: String
    public var phoneID: String         // this phone, as the hub knows it

    /// Where the Keychain keeps this phone's token on the hub.
    public static let tokenID = UUID(uuidString: "0B5E55ED-0000-4000-8000-00000000ACC7")!

    /// The one Kelvin hub, for every Kelvin user in every lab (as central.DEFAULT_HUB in Kelvin). It moves with an
    /// update of the app when the hub moves to its permanent server.
    public static let defaultHub = URL(string: "https://kelvin.tail269ee5.ts.net")!

    public init(hub: URL, email: String, name: String, phoneID: String) {
        self.hub = hub
        self.email = email
        self.name = name
        self.phoneID = phoneID
    }

    public static func decode(_ data: Data?) -> Account? {
        guard let data else { return nil }
        return try? JSONDecoder().decode(Account.self, from: data)
    }

    public func encoded() -> Data { (try? JSONEncoder().encode(self)) ?? Data() }

    /// The hub's address as people type it: "kelvin.example.org" means https; http only for this phone itself.
    public static func hubAddress(_ typed: String) -> URL? {
        var text = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        while text.hasSuffix("/") { text.removeLast() }
        guard !text.isEmpty, !text.contains(" ") else { return nil }
        if !text.contains("://") { text = "https://" + text }
        guard let url = URL(string: text), let host = url.host, !host.isEmpty else { return nil }
        if url.scheme == "https" { return url }
        if url.scheme == "http" && ["127.0.0.1", "localhost", "::1"].contains(host) { return url }
        return nil
    }
}

/// How this phone introduces itself to the hub.
public struct PhoneInfo: Equatable, Sendable {
    public var id: String              // "ios-" and a UUID, made once and kept
    public var name: String
    public var version: String

    public init(id: String, name: String, version: String) {
        self.id = id
        self.name = name
        self.version = version
    }

    public static func newID() -> String { "ios-" + UUID().uuidString }
}

/// One of the account's Kelvins, as the hub lists them.
public struct HubKelvin: Decodable, Equatable, Identifiable, Sendable {
    public var id: String              // its installation id on the hub
    public var name: String            // the computer's name
    public var platform: String
    public var version: String?
    public var online: Bool            // reachable through the hub now
    public var direct: Bool            // its phone access is on, so a phone can also reach it directly
    public var lastSeen: Double?

    enum CodingKeys: String, CodingKey { case id, name, platform, version, online, direct, lastSeen = "last_seen" }

    public init(id: String, name: String, platform: String = "win32", version: String? = nil, online: Bool = true,
                direct: Bool = false, lastSeen: Double? = nil) {
        self.id = id
        self.name = name
        self.platform = platform
        self.version = version
        self.online = online
        self.direct = direct
        self.lastSeen = lastSeen
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = c.value(.name, or: "Kelvin")
        platform = c.value(.platform, or: "")
        version = c.maybe(.version)
        online = c.value(.online, or: false)
        direct = c.value(.direct, or: false)
        lastSeen = c.maybe(.lastSeen)
    }
}

/// POST /api/phone/direct through the hub: this phone's own token for reaching that Kelvin directly.
public struct DirectAccess: Decodable, Equatable, Sendable {
    public var available: Bool
    public var token: String?
    public var addresses: [String]
    public var kinds: [String: String]
    public var reason: String?

    enum CodingKeys: String, CodingKey { case available, token, addresses, routes, reason }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, or: false)
        token = c.maybe(.token)
        addresses = c.value(.addresses, or: [])
        reason = c.maybe(.reason)
        let routes: [JSONValue] = c.value(.routes, or: [])
        kinds = Dictionary(routes.compactMap { r in r["url"]?.string.flatMap { u in r["kind"]?.string.map { (u, $0) } } },
                           uniquingKeysWith: { a, _ in a })
    }
}

/// The phone's side of the Kelvin hub: sign in with the Kelvin account, list its Kelvins, sign out.
public struct HubClient: Sendable {
    public var hub: URL
    public var token: String?
    public var transport: Transport
    public var timeout: TimeInterval

    public init(hub: URL, token: String? = nil, transport: Transport, timeout: TimeInterval = 15) {
        self.hub = hub
        self.token = token
        self.transport = transport
        self.timeout = timeout
    }

    /// Where a phone's calls to one of the account's Kelvins go: Kelvin's own routes follow this.
    public func relayBase(_ kelvin: String) -> URL {
        hub.appendingPathComponent("api/relay/kelvins").appendingPathComponent(kelvin)
    }

    /// Sign in with the Kelvin account; the hub answers with this phone's own token.
    public func signIn(email: String, password: String, phone: PhoneInfo) async throws -> (token: String, account: Account) {
        let installation = JSONValue.object(["id": .string(phone.id), "name": .string(phone.name),
                                             "platform": .string("ios"), "version": .string(phone.version)])
        let body = JSONValue.object(["email": .string(email), "password": .string(password), "installation": installation])
        let answer = try await call("POST", "/api/signin", body: body)
        guard let token = answer["access_token"]?.string else {
            throw KelvinError(.unreadable, "The Kelvin hub's answer was not what this app expects. Update the app.")
        }
        let user = answer["user"]
        return (token, Account(hub: hub, email: user?["email"]?.string ?? email, name: user?["name"]?.string ?? email,
                               phoneID: phone.id))
    }

    /// The account's Kelvins: online ones are reachable through the hub now.
    public func kelvins() async throws -> [HubKelvin] {
        let answer = try await call("GET", "/api/relay/kelvins")
        guard let list = answer["kelvins"] else { return [] }
        let data = try JSONEncoder().encode(list)
        return (try? JSONDecoder().decode([HubKelvin].self, from: data)) ?? []
    }

    /// End this phone's sign-in on the hub (its token stops working). Best effort: the phone forgets it anyway.
    public func signOut() async {
        _ = try? await call("POST", "/api/signout")
    }

    /// Delete the Kelvin account on the hub, for every computer and phone: with its password, as on the account page.
    public func deleteAccount(email: String, password: String) async throws {
        let body = JSONValue.object(["email": .string(email), "password": .string(password)])
        _ = try await call("POST", "/api/account/delete", body: body,
                           missing: "This Kelvin hub can't delete accounts from the app yet. Delete it on your account "
                               + "page: " + hub.appendingPathComponent("account").absoluteString)
    }

    /// missing: what to say when the hub doesn't have this yet (an older hub answers 404).
    func call(_ method: String, _ path: String, body: JSONValue? = nil, missing: String? = nil) async throws -> JSONValue {
        var r = URLRequest(url: hub.appendingPathComponent(String(path.dropFirst())),
                           cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        r.httpMethod = method
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token { r.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try? JSONEncoder().encode(body)
        }
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.data(for: r)
        } catch {
            let why = KelvinError.unreachable(hub, error).message
            throw KelvinError(.unreachable, why.replacingOccurrences(of: "Can't reach Kelvin at", with: "Can't reach the Kelvin hub at"))
        }
        let json = (try? JSONDecoder().decode(JSONValue.self, from: data)) ?? .object([:])
        guard (200..<300).contains(response.statusCode) else {
            let said = json["error"]?.string
            switch response.statusCode {
            case 401: throw KelvinError(.unpaired, said ?? "Wrong email or password.")
            case 403: throw KelvinError(.forbidden, said ?? "The Kelvin hub said no.")
            case 429: throw KelvinError(.locked, said ?? "Too many tries. Wait 15 minutes.")
            case 404 where missing != nil: throw KelvinError(.server, missing ?? "")
            default: throw KelvinError(.server, said ?? "The Kelvin hub answered HTTP \(response.statusCode).")
            }
        }
        return json
    }
}
