import Foundation

// The lab's team chat (Kelvin 0.3.32 and later): what the people who work there write to each other, kept on the
// Kelvin computer (src/labharness/teamchat.py). Everyone signed in there, and their phones, read the same chat, each
// writing under a name of their own; Kelvin reads it when asked, or when what the team said bears on the work.

/// One message.
public struct TeamMessage: Decodable, Equatable, Identifiable, Sendable {
    public var id: Int
    public var at: String         // the computer's time with its offset: 2026-10-30T09:12:44-04:00
    public var name: String       // the name its writer chose
    public var account: String    // the account signed in when it was sent
    public var via: String        // "portal", or "phone <its name>"
    public var text: String
    public var date: Date?        // `at`, if it reads

    enum CodingKeys: String, CodingKey { case id, at, name, account, via, text }

    public init(id: Int, at: String = "", name: String, account: String = "", via: String = "portal", text: String) {
        self.id = id
        self.at = at
        self.name = name
        self.account = account
        self.via = via
        self.text = text
        date = TeamMessage.time.date(from: at)
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        let at: String = c.value(.at, or: "")
        self.at = at
        name = c.value(.name, or: "")
        account = c.value(.account, or: "")
        via = c.value(.via, or: "portal")
        text = c.value(.text, or: "")
        date = TeamMessage.time.date(from: at)
    }

    private static let time: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
}

/// GET /api/team: messages, oldest first, and the newest one's id.
public struct TeamPage: Decodable, Equatable, Sendable {
    public var messages: [TeamMessage]
    public var last: Int

    enum CodingKeys: String, CodingKey { case messages, last }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let messages: [TeamMessage] = c.value(.messages, or: [])
        self.messages = messages
        last = c.value(.last, or: messages.last?.id ?? 0)
    }
}

/// POST /api/team: the message as Kelvin kept it.
struct TeamPosted: Decodable {
    var message: TeamMessage
}

/// The team chat as this phone has it: what it fetched, oldest first, each message once.
public struct TeamThread: Equatable, Sendable {
    /// What Kelvin sends at most at once (teamchat.PAGE): a full page may have older messages before it.
    public static let page = 200

    public private(set) var messages: [TeamMessage] = []
    /// Kelvin has messages older than these (scroll back for them).
    public private(set) var more = false
    /// Every message up to this one is here: the next fetch asks for what came after it. One this phone just sent may
    /// stand beyond it, until that fetch brings what others wrote meanwhile.
    public private(set) var through = 0

    public init() {}

    /// The newest message here (0: none yet).
    public var last: Int { messages.last?.id ?? 0 }

    /// What Kelvin answered to a fetch after `through`. False when its team chat began again (its newest message is
    /// older than those here): the thread is emptied, to fetch again from the start.
    @discardableResult
    public mutating func take(_ page: TeamPage) -> Bool {
        if page.last < through {
            self = TeamThread()
            return false
        }
        take(page.messages)
        return true
    }

    /// Messages fetched after `through` (the latest ones, the first time), or (older) a page from before the first.
    /// A full page of new ones starts the thread again from them, the ones between a scroll back away.
    public mutating func take(_ page: [TeamMessage], older: Bool = false) {
        if !older && page.count >= TeamThread.page { messages = [] }
        if older || messages.isEmpty { more = page.count >= TeamThread.page }
        if !older, let newest = page.map(\.id).max() { through = max(through, newest) }
        merge(page)
    }

    /// A message this phone just sent, shown at once.
    public mutating func sent(_ message: TeamMessage) { merge([message]) }

    private mutating func merge(_ page: [TeamMessage]) {
        let have = Set(messages.map(\.id))
        let fresh = page.filter { !have.contains($0.id) }
        guard !fresh.isEmpty else { return }
        messages = (messages + fresh).sorted { $0.id < $1.id }
    }

    /// A message as the screen shows it: under its day when it is the first of that day, and without its name and
    /// time when it follows the same person's within five minutes.
    public struct Line: Equatable, Identifiable, Sendable {
        public var message: TeamMessage
        public var day: Date?
        public var continued: Bool

        public var id: Int { message.id }
    }

    public func lines(calendar: Calendar = .current) -> [Line] {
        var out: [Line] = []
        var previous: TeamMessage?
        var previousDate: Date?
        for m in messages {
            let date = m.date
            var newDay = previous == nil
            if let date, let before = previousDate { newDay = !calendar.isDate(date, inSameDayAs: before) }
            var continued = false
            if !newDay, let p = previous, p.name == m.name, let date, let before = previousDate {
                continued = date.timeIntervalSince(before) < 300
            }
            out.append(Line(message: m, day: newDay ? date : nil, continued: continued))
            previous = m
            previousDate = date
        }
        return out
    }
}
