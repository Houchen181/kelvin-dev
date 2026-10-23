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

    public func route(_ url: URL) -> Route { Route(url, kinds: kinds) }

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

    /// A new pairing. Pairing the same computer again (same name and an address in common) replaces the old one,
    /// whose token is then useless; returns the id it replaced, if any, so its token can be deleted.
    @discardableResult
    public mutating func add(_ c: Computer) -> UUID? {
        let old = computers.firstIndex { o in
            o.host != nil && o.host == c.host && !Set(o.addresses).isDisjoint(with: c.addresses)
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

    public mutating func update(_ c: Computer) {
        if let i = computers.firstIndex(where: { $0.id == c.id }) { computers[i] = c }
    }
}
