import Foundation

// What Kelvin's phone API answers (src/labharness/web/app.py). Every field a newer or older Kelvin might leave out
// is optional or has a default, so the app keeps working across versions.

extension KeyedDecodingContainer {
    func value<T: Decodable>(_ key: Key, or fallback: T) -> T {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
    }

    func maybe<T: Decodable>(_ key: Key) -> T? {
        (try? decodeIfPresent(T.self, forKey: key)) ?? nil
    }
}

/// POST /api/pair
public struct PairResult: Decodable, Equatable, Sendable {
    public var token: String
    public var name: String
    public var role: String
}

/// GET /api/me: who this phone is to Kelvin, and (Kelvin 0.3.15 and later) the computer's name and addresses.
public struct Me: Decodable, Equatable, Sendable {
    public var role: String
    public var name: String?
    public var lab: String?
    public var computer: String?
    public var version: String?
    public var api: Int?
    public var addresses: [String]
    public var kinds: [String: String]       // url -> tailscale, network or public (Kelvin 0.3.16 and later)

    enum CodingKeys: String, CodingKey { case role, name, lab, computer, version, api, addresses, routes }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        role = c.value(.role, or: "watch")
        name = c.maybe(.name)
        lab = c.maybe(.lab)
        computer = c.maybe(.computer)
        version = c.maybe(.version)
        api = c.maybe(.api)
        addresses = c.value(.addresses, or: [])
        let routes: [JSONValue] = c.value(.routes, or: [])
        kinds = Dictionary(routes.compactMap { r in r["url"]?.string.flatMap { u in r["kind"]?.string.map { (u, $0) } } },
                           uniquingKeysWith: { a, _ in a })
    }
}

public enum Role: String, Sendable {
    case watch, control

    public init(_ text: String) { self = text == "watch" ? .watch : .control }

    public var canChat: Bool { self == .control }
    public var canApprove: Bool { self == .control }

    public var summary: String {
        self == .watch ? "Can watch, stop runs and decline requests" : "Can also chat and approve"
    }
}

/// A request waiting for a person's answer.
public struct ApprovalRequest: Decodable, Equatable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var items: [String]
    public var secondsLeft: Int?

    enum CodingKeys: String, CodingKey { case id, title, items, secondsLeft = "seconds_left" }

    public init(id: String, title: String, items: [String], secondsLeft: Int? = nil) {
        self.id = id
        self.title = title
        self.items = items
        self.secondsLeft = secondsLeft
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = c.value(.title, or: "Kelvin asks")
        items = c.value(.items, or: [])
        secondsLeft = c.maybe(.secondsLeft)
    }
}

public struct Alarm: Decodable, Equatable, Identifiable, Sendable {
    public var id: String
    public var kind: String?
    public var key: String?
    public var text: String
    public var actions: [String]
    public var raised: Double?
    public var cleared: Double?
    public var acknowledged: Bool

    enum CodingKeys: String, CodingKey { case id, kind, key, text, actions, raised, cleared, acknowledged }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        kind = c.maybe(.kind)
        key = c.maybe(.key)
        text = c.value(.text, or: "")
        actions = c.value(.actions, or: [])
        raised = c.maybe(.raised)
        cleared = c.maybe(.cleared)
        acknowledged = c.value(.acknowledged, or: false)
    }

    public var open: Bool { cleared == nil && !acknowledged }
}

public struct Notice: Decodable, Equatable, Identifiable, Sendable {
    public var id: Int
    public var kind: String?
    public var title: String
    public var text: String?
    public var at: Double

    enum CodingKeys: String, CodingKey { case id, kind, title, text, at }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        at = c.value(.at, or: 0)
        id = c.value(.id, or: Int(at * 1000))
        kind = c.maybe(.kind)
        title = c.value(.title, or: "")
        text = c.maybe(.text)
    }
}

/// One parameter of an instrument as the Lab panel shows it. `away`: a drive that is not at its safe value.
public struct Reading: Decodable, Equatable, Identifiable, Sendable {
    public var name: String
    public var value: String
    public var away: Bool

    public var id: String { name }

    enum CodingKeys: String, CodingKey { case name, value, away }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.value(.name, or: "")
        value = (c.maybe(.value) as JSONValue?)?.text ?? ""
        away = c.value(.away, or: false)
    }
}

public struct Instrument: Equatable, Identifiable, Sendable {
    public var name: String
    public var readings: [Reading]

    public var id: String { name }
    public var away: Bool { readings.contains { $0.away } }
    public var failing: Bool { readings.contains { $0.value == "ERROR" } }
}

/// The run in progress, or the last one.
public struct RunProgress: Decodable, Equatable, Sendable {
    public var runID: String
    public var plan: String
    public var done: Int
    public var total: Int
    public var etaSeconds: Double?
    public var status: String

    enum CodingKeys: String, CodingKey { case runID = "run_id", plan, done, total, etaSeconds = "eta_s", status }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        runID = c.value(.runID, or: "")
        plan = c.value(.plan, or: "")
        done = c.value(.done, or: 0)
        total = c.value(.total, or: 0)
        etaSeconds = c.maybe(.etaSeconds)
        status = c.value(.status, or: "")
    }

    public var running: Bool { status == "running" }
    public var fraction: Double { total > 0 ? min(1, Double(done) / Double(total)) : 0 }
}

/// The conversation Kelvin is answering now, on any device.
public struct ActiveChat: Decodable, Equatable, Sendable {
    public var chatID: String
    public var model: String?

    enum CodingKeys: String, CodingKey { case chatID = "chat_id", model }
}

/// GET /api/state: the lab at a glance.
public struct LabState: Decodable, Equatable, Sendable {
    public var lab: String
    public var simulated: Bool
    public var busy: String?
    public var pending: [ApprovalRequest]
    public var alarms: [Alarm]
    public var notices: [Notice]
    public var status: [String: [Reading]]
    public var live: RunProgress?
    public var active: ActiveChat?
    public var approvalMode: String?
    public var observeOnly: Bool

    enum CodingKeys: String, CodingKey {
        case lab, simulated, busy, pending, alarms, notices, status, live, active
        case approvalMode = "approval_mode", observeOnly = "observe_only"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        lab = c.value(.lab, or: "Kelvin")
        simulated = c.value(.simulated, or: false)
        busy = c.maybe(.busy)
        pending = c.value(.pending, or: [])
        alarms = c.value(.alarms, or: [])
        notices = c.value(.notices, or: [])
        status = c.value(.status, or: [:])
        live = c.maybe(.live)
        active = c.maybe(.active)
        approvalMode = c.maybe(.approvalMode)
        observeOnly = c.value(.observeOnly, or: false)
    }

    /// Instruments by name (JSON objects carry no order a decoder keeps).
    public var instruments: [Instrument] {
        status.keys.sorted { $0.lowercased() < $1.lowercased() }.map { Instrument(name: $0, readings: status[$0] ?? []) }
    }

    public var openAlarms: [Alarm] { alarms.filter(\.open) }

    /// What the lab tab's badge counts: requests and alarms waiting for a person.
    public var waiting: Int { pending.count + openAlarms.count }

    public var activity: String {
        if let busy, !busy.isEmpty { return "Kelvin is " + busy }
        if let live, live.running { return "Running " + live.plan }
        return "Kelvin is idle"
    }
}

/// A column of a run: a swept axis or a measured value.
public struct Column: Decodable, Equatable, Hashable, Sendable {
    public var key: String
    public var unit: String
    public var kind: String
    public var xUnit: String?

    enum CodingKeys: String, CodingKey { case key, unit, kind, xUnit = "x_unit" }

    public init(key: String, unit: String = "", kind: String = "scalar") {
        self.key = key
        self.unit = unit
        self.kind = kind
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.value(.key, or: "")
        unit = c.value(.unit, or: "")
        kind = c.value(.kind, or: "scalar")
        xUnit = c.maybe(.xUnit)
    }

    /// "lockin.x (V)"
    public var label: String { unit.isEmpty ? key : "\(key) (\(unit))" }
}

/// The latest spectrum or trace of a run.
public struct Trace: Decodable, Equatable, Sendable {
    public var key: String
    public var unit: String
    public var xUnit: String
    public var points: [Point]

    enum CodingKeys: String, CodingKey { case key, unit, xUnit = "x_unit", x, y }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.value(.key, or: "")
        unit = c.value(.unit, or: "")
        xUnit = c.value(.xUnit, or: "")
        let xs: [JSONValue] = c.value(.x, or: [])
        let ys: [JSONValue] = c.value(.y, or: [])
        points = zip(xs, ys).compactMap { x, y in
            guard let x = x.double, let y = y.double else { return nil }
            return Point(x: x, y: y)
        }
    }
}

public struct Point: Equatable, Hashable, Sendable {
    public var x: Double
    public var y: Double

    public init(x: Double, y: Double) {
        self.x = x
        self.y = y
    }
}

/// GET /api/live: the running (or last) plan with the rows recorded since `since`.
public struct LiveRun: Decodable, Equatable, Sendable {
    public var runID: String
    public var plan: String
    public var total: Int
    public var done: Int
    public var etaSeconds: Double?
    public var status: String
    public var phase: String?
    public var axes: [Column]
    public var measures: [Column]
    public var rows: [[String: JSONValue]]
    public var trace: Trace?

    enum CodingKeys: String, CodingKey {
        case runID = "run_id", plan, total, done, etaSeconds = "eta_s", status, phase, axes, measures, rows, trace
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        runID = c.value(.runID, or: "")
        plan = c.value(.plan, or: "")
        total = c.value(.total, or: 0)
        done = c.value(.done, or: 0)
        etaSeconds = c.maybe(.etaSeconds)
        status = c.value(.status, or: "")
        phase = c.maybe(.phase)
        axes = c.value(.axes, or: [])
        measures = c.value(.measures, or: [])
        rows = c.value(.rows, or: [])
        trace = c.maybe(.trace)
    }
}

struct LiveAnswer: Decodable {
    var live: LiveRun?
}

/// GET /api/runs
public struct RunSummary: Decodable, Equatable, Identifiable, Sendable {
    public var runID: String
    public var plan: String
    public var status: String
    public var points: Int?
    public var plannedPoints: Int?
    public var reason: String?

    public var id: String { runID }

    enum CodingKeys: String, CodingKey {
        case runID = "run_id", plan, status, points, plannedPoints = "planned_points", reason
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        runID = try c.decode(String.self, forKey: .runID)
        plan = c.value(.plan, or: runID)
        status = c.value(.status, or: "unfinished")
        points = c.maybe(.points)
        plannedPoints = c.maybe(.plannedPoints)
        reason = c.maybe(.reason)
    }

    /// Run ids start with the time they began: 20261022-143005-plan.
    public var started: Date? {
        let stamp = runID.split(separator: "-").prefix(2).joined(separator: "-")
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.date(from: stamp)
    }
}

struct RunsAnswer: Decodable {
    var runs: [RunSummary]
}

/// GET /api/runs/{id}: what a plot of the run needs, and how it ended.
public struct RunDetail: Decodable, Equatable, Sendable {
    public struct Meta: Decodable, Equatable, Sendable {
        public var status: String?
        public var reason: String?
        public var points: Int?
        public var plannedPoints: Int?
        public var durationSeconds: Double?
        public var warnings: [String]

        enum CodingKeys: String, CodingKey {
            case status, reason, points, plannedPoints = "planned_points", durationSeconds = "duration_s", warnings
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            status = c.maybe(.status)
            reason = c.maybe(.reason)
            points = c.maybe(.points)
            plannedPoints = c.maybe(.plannedPoints)
            durationSeconds = c.maybe(.durationSeconds)
            warnings = ((c.maybe(.warnings) as [JSONValue]?) ?? []).map(\.text)
        }
    }

    public var runID: String
    public var plan: String
    public var axes: [Column]
    public var scalars: [Column]
    public var rows: [[String: JSONValue]]
    public var traces: [Trace]
    public var planText: String?
    public var meta: Meta?
    public var files: [String]

    enum CodingKeys: String, CodingKey {
        case runID = "run_id", plan, axes, scalars, rows, traces, planText = "plan_text", meta, files
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        runID = c.value(.runID, or: "")
        plan = c.value(.plan, or: "")
        axes = c.value(.axes, or: [])
        scalars = c.value(.scalars, or: [])
        rows = c.value(.rows, or: [])
        traces = c.value(.traces, or: [])
        planText = c.maybe(.planText)
        meta = c.maybe(.meta)
        let listed: [JSONValue] = c.value(.files, or: [])
        files = listed.compactMap { $0.string ?? $0["name"]?.string }
    }
}

/// GET /api/chats
public struct ChatSummary: Decodable, Equatable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var updated: Double
    public var model: String?
    public var incognito: Bool

    enum CodingKeys: String, CodingKey { case id, title, updated, model, incognito }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = c.value(.title, or: "Chat")
        updated = c.value(.updated, or: 0)
        model = c.maybe(.model)
        incognito = c.value(.incognito, or: false)
    }
}

/// GET /api/chats/{id}: the conversation as Kelvin keeps it.
public struct ChatDetail: Decodable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var model: String?
    public var incognito: Bool
    public var messages: [JSONValue]

    enum CodingKeys: String, CodingKey { case id, title, model, incognito, messages }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = c.value(.title, or: "Chat")
        model = c.maybe(.model)
        incognito = c.value(.incognito, or: false)
        messages = c.value(.messages, or: [])
    }
}

/// GET /api/bootstrap: only the models, for starting a chat.
public struct Models: Decodable, Equatable, Sendable {
    public struct Model: Decodable, Equatable, Identifiable, Hashable, Sendable {
        public var key: String
        public var label: String
        public var id: String { key }
    }

    public var all: [Model]
    public var defaultModel: String?

    enum CodingKeys: String, CodingKey { case models, defaultModel = "default_model" }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let models: JSONValue = c.value(.models, or: .null)
        all = (models["groups"]?.array ?? []).flatMap { group in
            (group["models"]?.array ?? []).compactMap { m in
                m["key"]?.string.map { Model(key: $0, label: m["label"]?.string ?? $0) }
            }
        }
        defaultModel = c.maybe(.defaultModel)
    }

    public func label(_ key: String?) -> String? {
        guard let key else { return nil }
        return all.first { $0.key == key }?.label ?? key.split(separator: ":").last.map(String.init)
    }
}
