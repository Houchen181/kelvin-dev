import Foundation

/// A Kelvin computer this phone is paired with. Nothing secret: the token lives in the Keychain under `id`.
public struct Computer: Codable, Equatable, Identifiable, Sendable {
    public var id: UUID
    public var lab: String              // the lab's name, as Kelvin calls it
    public var host: String?            // the computer's own name
    public var phoneName: String        // this phone, as Kelvin knows it
    public var role: String
    public var addresses: [URL]         // where to try, the one that last worked first
    public var paired: Date
    public var version: String?
    public var kinds: [String: String]?   // what Kelvin says each address is (see Route)
    // Through the Kelvin account (Account.swift): the hub that reaches it, and its id there. Its addresses are then
    // the direct ones, tried first, with the token it gave this phone through the hub.
    public var hub: URL?
    public var kelvin: String?
    // A paired computer the Kelvin account reaches too (ComputerList.sync): its one row is the pairing, and away
    // from the lab, without Tailscale, the phone reaches it through the hub with the account's token.
    public var accountHub: URL?
    public var accountKelvin: String?

    public init(id: UUID = UUID(), lab: String, host: String? = nil, phoneName: String, role: String,
                addresses: [URL], paired: Date = Date(), version: String? = nil) {
        self.id = id
        self.lab = lab
        self.host = host
        self.phoneName = phoneName
        self.role = role
        self.addresses = addresses
        self.paired = paired
        self.version = version
    }

    public var access: Role { Role(role) }

    /// Through the person's Kelvin account, rather than paired.
    public var viaAccount: Bool { hub != nil && kelvin != nil }

    /// The hub's way to this Kelvin, when it comes through the account.
    public var relayBase: URL? {
        guard let hub, let kelvin else { return nil }
        return hub.appendingPathComponent("api/relay/kelvins").appendingPathComponent(kelvin)
    }

    public func route(_ url: URL) -> Route { Route(url, kinds: kinds) }

    /// The hub's way to a paired computer the account reaches too.
    public var accountRelay: URL? {
        guard let accountHub, let accountKelvin else { return nil }
        return accountHub.appendingPathComponent("api/relay/kelvins").appendingPathComponent(accountKelvin)
    }

    /// "Cryostat lab" or, when two computers share a lab name, "Cryostat lab (LAB-PC-2)".
    public func title(among all: [Computer]) -> String {
        let twins = all.filter { $0.lab == lab }.count > 1
        return twins ? "\(lab) (\(host ?? addresses.first?.host ?? "?"))" : lab
    }

    /// After a successful connection at `used`: keep it first, then what Kelvin says its other addresses are.
    public mutating func update(with me: Me, used: URL) {
        if let name = me.lab, !name.isEmpty { lab = name }
        if let h = me.computer, !h.isEmpty { host = h }
        if let n = me.name, !n.isEmpty { phoneName = n }
        role = me.role
        version = me.version ?? version
        let told = me.addresses.compactMap { URL(string: $0) }
        addresses = Computer.ordered([used] + told + addresses)
        if !me.kinds.isEmpty { kinds = (kinds ?? [:]).merging(me.kinds) { _, new in new } }
    }

    /// Remember which address worked, so the next connection tries it first.
    public mutating func worked(at used: URL) {
        addresses = Computer.ordered([used] + addresses)
    }

    /// Without repeats, and at most eight (old addresses fall off the end).
    static func ordered(_ urls: [URL]) -> [URL] {
        var seen = Set<String>()
        var out: [URL] = []
        for u in urls where seen.insert(u.absoluteString.lowercased()).inserted {
            out.append(u)
        }
        return Array(out.prefix(8))
    }
}

/// The paired computers, kept as JSON (in UserDefaults on the phone).
public struct ComputerList: Codable, Equatable, Sendable {
    public var computers: [Computer] = []
    public var selected: UUID?

    public init(computers: [Computer] = [], selected: UUID? = nil) {
        self.computers = computers
        self.selected = selected
    }

    public static func decode(_ data: Data?) -> ComputerList {
        guard let data, let list = try? JSONDecoder().decode(ComputerList.self, from: data) else { return ComputerList() }
        return list
    }

    public func encoded() -> Data { (try? JSONEncoder().encode(self)) ?? Data() }

    public var current: Computer? {
        computers.first { $0.id == selected } ?? computers.first
    }

    /// A computer added on this phone: the demo lab, or (before app 0.1.8) a pairing. The same computer again replaces
    /// the old one, whose token is then useless: the same computer by its name, wherever it is now (a laptop has
    /// another address on another network), or, when Kelvin didn't say its name, by an address in common. Returns the
    /// id it replaced, if any, so its token can be deleted.
    @discardableResult
    public mutating func add(_ c: Computer) -> UUID? {
        let old = computers.firstIndex { o in
            !o.viaAccount && (c.host != nil ? o.host == c.host
                                            : o.host == nil && !Set(o.addresses).isDisjoint(with: c.addresses))
        }
        var replaced: UUID?
        if let i = old {
            replaced = computers[i].id
            computers[i] = c
        } else {
            computers.append(c)
        }
        selected = c.id
        return replaced
    }

    public mutating func remove(_ id: UUID) {
        computers.removeAll { $0.id == id }
        if selected == id { selected = computers.first?.id }
    }

    /// A computer as a session saw it. Whether the account reaches it too is the list's to say (sync), whatever an
    /// older copy says.
    public mutating func update(_ c: Computer) {
        guard let i = computers.firstIndex(where: { $0.id == c.id }) else { return }
        var c = c
        c.accountHub = computers[i].accountHub
        c.accountKelvin = computers[i].accountKelvin
        computers[i] = c
    }

    /// Signed out of the account at `hub`: paired computers are reached only directly again.
    public mutating func forgetAccount(_ hub: URL) {
        for i in computers.indices where computers[i].accountHub == hub {
            computers[i].accountHub = nil
            computers[i].accountKelvin = nil
        }
    }

    /// One of the account's Kelvins, as the hub lists it: a new one is added (and shown if nothing else is), one
    /// this phone already has keeps its id, so its direct token stays.
    @discardableResult
    public mutating func account(_ k: HubKelvin, hub: URL) -> Computer {
        if let i = computers.firstIndex(where: { $0.kelvin == k.id && $0.hub == hub }) {
            if computers[i].host != k.name { computers[i].host = k.name }
            if let v = k.version { computers[i].version = v }
            return computers[i]
        }
        var c = Computer(lab: k.name, host: k.name, phoneName: "This phone", role: "control", addresses: [],
                         version: k.version)
        c.hub = hub
        c.kelvin = k.id
        computers.append(c)
        if selected == nil || !computers.contains(where: { $0.id == selected }) { selected = c.id }
        return c
    }

    /// The account's Kelvins as the hub lists them, one row per computer: the hub keeps every Kelvin that ever signed
    /// in with the account until it signs out, so a computer can have had several (a new data folder, a reinstall).
    /// Of a computer's, those online now are shown, else its latest; none of a computer removed on this phone (nor its
    /// older ones), nor of one this phone is paired with (its row is the pairing), nor those the hub no longer lists.
    /// Returns the ids that left the list, so their tokens go too.
    @discardableResult
    public mutating func sync(_ listed: [HubKelvin], hub: URL, removed: Set<String> = []) -> [UUID] {
        let removedNames = Set(listed.filter { removed.contains($0.id) }.map { $0.name.lowercased() })
        let paired = Set(computers.filter { !$0.viaAccount }.compactMap { $0.host?.lowercased() })
        let kept = listed.filter { !removedNames.contains($0.name.lowercased()) && !paired.contains($0.name.lowercased()) }
        let shown = kept.filter { k in
            let same = kept.filter { $0.name == k.name }
            if same.contains(where: \.online) { return k.online }
            return same.max { ($0.lastSeen ?? 0) < ($1.lastSeen ?? 0) }?.id == k.id
        }
        for k in shown { account(k, hub: hub) }
        for i in computers.indices where !computers[i].viaAccount {   // a paired one: the hub for when it's away
            guard let host = computers[i].host?.lowercased() else { continue }
            let same = listed.filter { $0.name.lowercased() == host && !removed.contains($0.id) }
            let k = same.first(where: \.online) ?? same.max { ($0.lastSeen ?? 0) < ($1.lastSeen ?? 0) }
            computers[i].accountHub = k == nil ? nil : hub
            computers[i].accountKelvin = k?.id
        }
        let ids = Set(shown.map(\.id))
        let gone = computers.filter { c in c.hub == hub && c.kelvin.map { !ids.contains($0) } ?? false }.map(\.id)
        for id in gone { remove(id) }
        return gone
    }

    /// Older pairings of a computer paired again before a pairing replaced them by its name (app 0.1.6 and before):
    /// the one in use, else the newest, stays. Returns the ids that left the list, so their tokens go too.
    @discardableResult
    public mutating func tidy() -> [UUID] {
        var gone: [UUID] = []
        for c in computers where !c.viaAccount && c.host != nil && !gone.contains(c.id) {
            let same = computers.filter { !$0.viaAccount && $0.host == c.host }
            guard same.count > 1 else { continue }
            let stay = same.first { $0.id == selected } ?? same.max { $0.paired < $1.paired } ?? c
            gone += same.filter { $0.id != stay.id && !gone.contains($0.id) }.map(\.id)
        }
        for id in gone { remove(id) }
        return gone
    }
}
