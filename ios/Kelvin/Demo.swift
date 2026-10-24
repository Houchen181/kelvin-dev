import Foundation
import KelvinKit

typealias JSON = [String: Any]

/// A made-up lab inside the app, to try it without a Kelvin computer (and for App Review, which has none).
/// It answers the same routes as Kelvin's phone listener; nothing leaves the phone.
final class DemoTransport: Transport, @unchecked Sendable {
    static let shared = DemoTransport()
    static let address = URL(string: "http://demo.invalid")!
    // run ids start with the time a run began: the demo's runs began just now, three hours ago and yesterday
    static let runID = stamp(30, "iv-sweep-D3")
    static let fieldRunID = stamp(3 * 3600, "field-sweep-D3")
    static let cooldownRunID = stamp(26 * 3600, "cooldown")

    private static func stamp(_ secondsAgo: TimeInterval, _ plan: String) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.string(from: Date(timeIntervalSinceNow: -secondsAgo)) + "-" + plan
    }
    static let total = 41

    private let lock = NSLock()
    private let started = Date()
    private var pending = true
    private var alarm = true
    private var stopped: Int?

    // ------------------------------------------------------------------ the lab

    private func done() -> Int {
        if let stopped { return stopped }
        return min(DemoTransport.total, 12 + Int(Date().timeIntervalSince(started) / 2))
    }

    private func running() -> Bool { stopped == nil && done() < DemoTransport.total }

    private static func row(_ i: Int) -> JSON {
        let v = -0.5 + Double(i) * 0.025
        var row: JSON = [:]
        row["i"] = i
        row["t"] = Double(i) * 2
        row["smu.voltage"] = v
        row["smu.current"] = 4e-8 * sinh(v / 0.15)
        row["lockin.x"] = 3.1e-6 * (1 + 0.4 * v * v)
        return row
    }

    private static func reading(_ name: String, _ value: String, _ away: Bool = false) -> JSON {
        var r: JSON = [:]
        r["name"] = name
        r["value"] = value
        r["away"] = away
        return r
    }

    private func state() -> JSON {
        let n = done()
        let now = Date().timeIntervalSince1970
        let r = DemoTransport.reading
        var s: JSON = [:]
        s["lab"] = "Demo lab"
        s["simulated"] = true
        s["approval_mode"] = "ask"
        if running() { s["busy"] = "running iv-sweep-D3" }

        var asks: [JSON] = []
        if pending {
            var ask: JSON = [:]
            ask["id"] = "demo-ask"
            ask["title"] = "Raise the magnet to 1 T"
            ask["items"] = ["magnet.field 0.50 T → 1.00 T, ramped at 0.1 T/min"]
            ask["seconds_left"] = 240
            asks.append(ask)
        }
        s["pending"] = asks

        var alarms: [JSON] = []
        if alarm {
            var a: JSON = [:]
            a["id"] = "demo-alarm"
            a["kind"] = "reading"
            a["key"] = "lockin.x"
            a["text"] = "lockin.x overloaded for 5 s"
            a["raised"] = now - 95
            a["actions"] = ["lowered lockin.sensitivity"]
            a["acknowledged"] = false
            alarms.append(a)
        }
        s["alarms"] = alarms

        var first: JSON = [:]
        first["id"] = 1
        first["title"] = "field-sweep-D3 finished: 121 of 121 points"
        first["at"] = now - 7200       // the field sweep began three hours ago and took an hour
        var second: JSON = [:]
        second["id"] = 2
        second["title"] = "Approved: smu.voltage to 0.5 V"
        second["at"] = now - 600
        s["notices"] = [first, second]

        let volts = String(format: "%.3f V", -0.5 + Double(max(0, n - 1)) * 0.025)
        var status: [String: [JSON]] = [:]
        status["cryostat"] = [r("temperature", "1.52 K", false), r("setpoint", "1.50 K", false), r("heater", "0.8 %", false)]
        status["magnet"] = [r("field", "0.50 T", true), r("ramp_rate", "0.10 T/min", false)]
        status["lockin"] = [r("x", "3.21 µV", false), r("y", "0.08 µV", false), r("frequency", "13.333 Hz", false),
                            r("sensitivity", "10 µV", false)]
        status["smu"] = [r("voltage", volts, running()), r("current", "18.7 nA", false), r("compliance", "1 µA", false)]
        s["status"] = status

        var live: JSON = [:]
        live["run_id"] = DemoTransport.runID
        live["plan"] = "iv-sweep-D3"
        live["done"] = n
        live["total"] = DemoTransport.total
        live["eta_s"] = running() ? Double(DemoTransport.total - n) * 2 : 0.0
        live["status"] = stopped != nil ? "stopped" : running() ? "running" : "completed"
        s["live"] = live
        return s
    }

    private static func column(_ key: String, _ unit: String, _ kind: String = "scalar") -> JSON {
        var c: JSON = [:]
        c["key"] = key
        c["unit"] = unit
        c["kind"] = kind
        return c
    }

    private func live(since: Int) -> JSON {
        let n = done()
        var live: JSON = [:]
        live["run_id"] = DemoTransport.runID
        live["plan"] = "iv-sweep-D3"
        live["total"] = DemoTransport.total
        live["done"] = n
        live["status"] = running() ? "running" : "completed"
        live["axes"] = [DemoTransport.column("smu.voltage", "V", "sweep")]
        live["measures"] = [DemoTransport.column("smu.current", "A"), DemoTransport.column("lockin.x", "V")]
        live["rows"] = (min(since, n)..<n).map(DemoTransport.row)
        return ["live": live]
    }

    private func runs() -> JSON {
        func run(_ id: String, _ plan: String, _ status: String, _ points: Int?) -> JSON {
            var r: JSON = [:]
            r["run_id"] = id
            r["plan"] = plan
            r["status"] = status
            if let points {
                r["points"] = points
                r["planned_points"] = points
            }
            return r
        }
        let list: [JSON] = [run(DemoTransport.runID, "iv-sweep-D3", running() ? "running" : "completed", nil),
                            run(DemoTransport.fieldRunID, "field-sweep-D3", "completed", 121),
                            run(DemoTransport.cooldownRunID, "cooldown", "completed", 60)]
        return ["runs": list]
    }

    private func run(_ id: String) -> JSON {
        var d: JSON = [:]
        var meta: JSON = [:]
        d["run_id"] = id
        d["traces"] = [JSON]()
        d["files"] = [JSON]()
        meta["warnings"] = [String]()
        if id.hasSuffix("field-sweep-D3") {
            var rows: [JSON] = []
            for i in 0..<121 {
                let b = -3 + Double(i) * 0.05
                var row: JSON = [:]
                row["magnet.field"] = b
                row["lockin.x"] = 2.0e-6 * (1 + 0.08 * b * b) + 1e-7 * cos(6 * b)
                rows.append(row)
            }
            d["plan"] = "field-sweep-D3"
            d["axes"] = [DemoTransport.column("magnet.field", "T")]
            d["scalars"] = [DemoTransport.column("lockin.x", "V")]
            d["rows"] = rows
            d["plan_text"] = "plan: field-sweep-D3\naxes:\n  - sweep: magnet.field\n    from: -3 T\n    to: 3 T\n    points: 121\nmeasure: [lockin.x]\n"
            meta["status"] = "completed"
            meta["points"] = 121
            meta["planned_points"] = 121
            meta["duration_s"] = 3480.0
        } else {
            let n = done()
            d["plan"] = "iv-sweep-D3"
            d["axes"] = [DemoTransport.column("smu.voltage", "V")]
            d["scalars"] = [DemoTransport.column("smu.current", "A"), DemoTransport.column("lockin.x", "V")]
            d["rows"] = (0..<n).map(DemoTransport.row)
            d["plan_text"] = "plan: iv-sweep-D3\naxes:\n  - sweep: smu.voltage\n    from: -0.5 V\n    to: 0.5 V\n    points: 41\nmeasure: [smu.current, lockin.x]\n"
            meta["status"] = running() ? "running" : "completed"
            meta["points"] = n
            meta["planned_points"] = DemoTransport.total
        }
        d["meta"] = meta
        return d
    }

    private static func message(_ role: String, _ fields: JSON) -> JSON {
        var m = fields
        m["role"] = role
        return m
    }

    private func chat() -> JSON {
        var text: JSON = [:]
        text["type"] = "text"
        text["text"] = "The cryostat is at **1.52 K** and the magnet at 0.5 T. I'll sweep the SMU in 41 points with the lock-in on."
        var read: JSON = [:]
        read["type"] = "tool_call"
        read["id"] = "d1"
        read["name"] = "get_value"
        read["input"] = ["instrument": "cryostat", "parameter": "temperature"]
        var start: JSON = [:]
        start["type"] = "tool_call"
        start["id"] = "d2"
        start["name"] = "run_plan"
        start["input"] = ["plan": "plan: iv-sweep-D3\n"]
        var readResult: JSON = [:]
        readResult["id"] = "d1"
        readResult["content"] = "cryostat.temperature = 1.52 K"
        readResult["is_error"] = false
        var startResult: JSON = [:]
        startResult["id"] = "d2"
        startResult["content"] = "started run \(DemoTransport.runID) (41 points)"
        startResult["is_error"] = false
        var c: JSON = [:]
        c["id"] = "demo-chat"
        c["title"] = "IV of device D3"
        c["model"] = "demo:kelvin"
        c["messages"] = [DemoTransport.message("user", ["text": "Take an IV of D3 from -0.5 to 0.5 V at 1.5 K"]),
                         DemoTransport.message("assistant", ["blocks": [text, read, start]]),
                         DemoTransport.message("tool", ["results": [readResult, startResult]])]
        return c
    }

    // ------------------------------------------------------------------ the routes

    private func answer(_ request: URLRequest) -> (Int, JSON) {
        let path = request.url?.path ?? ""
        let method = request.httpMethod ?? "GET"
        let ok: JSON = ["ok": true]
        lock.lock()
        defer { lock.unlock() }
        switch (method, path) {
        case ("POST", "/api/pair"):
            return (200, ["token": "demo", "name": "This phone", "role": "control"])
        case ("GET", "/api/me"):
            var me: JSON = ["role": "control", "name": "This phone", "lab": "Demo lab", "computer": "DEMO"]
            me["version"] = "demo"
            me["api"] = 1
            me["addresses"] = [DemoTransport.address.absoluteString]
            return (200, me)
        case ("GET", "/api/state"):
            return (200, state())
        case ("GET", "/api/live"):
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let since = items.first { $0.name == "since" }?.value.flatMap { Int($0) } ?? 0
            return (200, live(since: since))
        case ("GET", "/api/runs"):
            return (200, runs())
        case ("GET", _) where path.hasPrefix("/api/runs/") && path.contains("/files/"):
            return (404, ["error": "The demo lab has no files to open."])
        case ("GET", _) where path.hasPrefix("/api/runs/"):
            return (200, run(String(path.dropFirst("/api/runs/".count))))
        case ("POST", "/api/approvals/demo-ask"):
            pending = false
            return (200, ok)
        case ("POST", "/api/alarms/demo-alarm/ack"):
            alarm = false
            return (200, ok)
        case ("POST", "/api/runs/stop"):
            stopped = done()
            return (200, ok)
        case ("POST", "/api/stop"), ("POST", "/api/me/forget"):
            return (200, ok)
        case ("GET", "/api/bootstrap"):
            let model: JSON = ["key": "demo:kelvin", "label": "Demo"]
            let group: JSON = ["models": [model]]
            var boot: JSON = ["default_model": "demo:kelvin"]
            boot["models"] = ["groups": [group]] as JSON
            return (200, boot)
        case ("GET", "/api/chats/demo-chat"):
            return (200, chat())
        case ("POST", "/api/chats"):
            var c: JSON = ["id": "demo-new", "title": "New chat", "model": "demo:kelvin"]
            c["messages"] = [JSON]()
            return (200, c)
        case ("GET", _) where path.hasPrefix("/api/chats/"):
            var c: JSON = ["id": String(path.dropFirst("/api/chats/".count)), "title": "New chat"]
            c["messages"] = [JSON]()
            return (200, c)
        default:
            return (404, ["error": "The demo lab doesn't do that."])
        }
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let status: Int
        let body: Any
        if request.httpMethod == "GET", request.url?.path == "/api/chats" {   // a list, not an object
            var c: JSON = ["id": "demo-chat", "title": "IV of device D3", "model": "demo:kelvin"]
            c["updated"] = Date().timeIntervalSince1970 - 900
            status = 200
            body = [c]
        } else {
            let a = answer(request)
            status = a.0
            body = a.1
        }
        let data = (try? JSONSerialization.data(withJSONObject: body)) ?? Data()
        return (data, HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }

    /// A chat answer, a few words at a time.
    func lines(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<String, Error>) {
        func event(_ type: String, _ fields: JSON = [:]) -> String {
            var e = fields
            e["type"] = type
            return (try? JSONSerialization.data(withJSONObject: e)).map { String(decoding: $0, as: UTF8.self) } ?? ""
        }
        var end: JSON = ["id": "t1", "name": "get_value", "content": "lockin.x = 3.21 µV"]
        end["is_error"] = false
        var start: JSON = ["id": "t1", "name": "get_value"]
        start["input"] = ["instrument": "lockin", "parameter": "x"]
        let lines = [
            event("turn_start", ["model": "demo:kelvin", "title": "Demo chat"]),
            event("text", ["delta": "This is the demo lab, so nothing real happens. "]),
            event("tool_start", start),
            event("tool_end", end),
            event("text", ["delta": "The lock-in reads **3.21 µV**. "]),
            event("text", ["delta": "Pair with Kelvin on your lab computer to talk to your own instruments."]),
            event("turn_end", ["reason": "end"]),
        ]
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        return (response, AsyncThrowingStream { continuation in
            let task = Task {
                for line in lines {
                    try? await Task.sleep(nanoseconds: 350_000_000)
                    continuation.yield(line)
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        })
    }
}
