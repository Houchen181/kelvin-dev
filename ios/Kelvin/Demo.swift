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
    private var hands: JSON?            // the hands-on-the-sample lock
    private var team: [JSON] = DemoTransport.firstTeam()
    private var chats: [JSON] = []      // chats begun here, the newest last, kept while the app runs
    private var begun = 0               // chats begun here, incognito ones too: each its own id

    // ------------------------------------------------------------------ the lab

    private func done() -> Int {
        if let stopped { return stopped }
        return min(DemoTransport.total, 12 + Int(Date().timeIntervalSince(started) / 2))
    }

    private func running() -> Bool { stopped == nil && done() < DemoTransport.total }

    /// The IV sweep's status, the same on every screen.
    private func runStatus() -> String { stopped != nil ? "stopped" : running() ? "running" : "completed" }

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

        var mounted: JSON = ["project": "demo-graphene", "project_name": "Graphene"]
        mounted["device"] = "demo-d3"
        mounted["device_name"] = "D3"
        mounted["protection_lines"] = ["Gate smu.voltage within ±6 V (20 nm hBN), stops above 1 nA on smu.current",
                                       "Capped in the instruments: smu.compliance ≤ 10 nA"]
        s["mounted"] = mounted
        if let hands { s["hands_on"] = hands }
        s["team"] = lastTeam()

        let volts = hands != nil ? "0.000 V" : String(format: "%.3f V", -0.5 + Double(max(0, n - 1)) * 0.025)
        var status: [String: [JSON]] = [:]
        status["cryostat"] = [r("temperature", "1.52 K", false), r("setpoint", "1.50 K", false), r("heater", "0.8 %", false)]
        status["magnet"] = [r("field", "0.50 T", true), r("ramp_rate", "0.10 T/min", false)]
        status["lockin"] = [r("x", "3.21 µV", false), r("y", "0.08 µV", false), r("frequency", "13.333 Hz", false),
                            r("sensitivity", "10 µV", false)]
        status["smu"] = [r("voltage", volts, running()), r("current", "18.7 nA", false), r("compliance", "10 nA", false)]
        s["status"] = status

        var live: JSON = [:]
        live["run_id"] = DemoTransport.runID
        live["plan"] = "iv-sweep-D3"
        live["done"] = n
        live["total"] = DemoTransport.total
        live["eta_s"] = running() ? Double(DemoTransport.total - n) * 2 : 0.0
        live["status"] = runStatus()
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
        live["status"] = runStatus()
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
        let list: [JSON] = [run(DemoTransport.runID, "iv-sweep-D3", runStatus(), nil),
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
                row["lockin.x"] = DemoTransport.resistance(b)
                row["lockin.y"] = DemoTransport.hall(b)
                rows.append(row)
            }
            d["plan"] = "field-sweep-D3"
            d["axes"] = [DemoTransport.column("magnet.field", "T")]
            d["scalars"] = [DemoTransport.column("lockin.x", "V"), DemoTransport.column("lockin.y", "V")]
            d["rows"] = rows
            d["plan_text"] = "plan: field-sweep-D3\naxes:\n  - sweep: magnet.field\n    from: -3 T\n    to: 3 T\n    points: 121\nmeasure: [lockin.x, lockin.y]\n"
            meta["status"] = "completed"
            meta["points"] = 121
            meta["planned_points"] = 121
            meta["duration_s"] = 3480.0
        } else if id.hasSuffix("cooldown") {
            d["plan"] = "cooldown"
            d["axes"] = [DemoTransport.column("t", "s")]
            d["scalars"] = [DemoTransport.column("cryostat.temperature", "K")]
            d["rows"] = (0..<60).map(DemoTransport.cooldown)
            d["plan_text"] = "plan: cooldown\nmonitor: [cryostat.temperature]\nevery: 6 min\nuntil: cryostat.temperature < 1.6 K\n"
            meta["status"] = "completed"
            meta["points"] = 60
            meta["planned_points"] = 60
            meta["duration_s"] = 21240.0
        } else {
            let n = done()
            d["plan"] = "iv-sweep-D3"
            d["axes"] = [DemoTransport.column("smu.voltage", "V")]
            d["scalars"] = [DemoTransport.column("smu.current", "A"), DemoTransport.column("lockin.x", "V")]
            d["rows"] = (0..<n).map(DemoTransport.row)
            d["plan_text"] = "plan: iv-sweep-D3\naxes:\n  - sweep: smu.voltage\n    from: -0.5 V\n    to: 0.5 V\n    points: 41\nmeasure: [smu.current, lockin.x]\n"
            meta["status"] = runStatus()
            meta["points"] = n
            meta["planned_points"] = DemoTransport.total
        }
        d["meta"] = meta
        return d
    }

    /// A chat begun here keeps what was said, as Kelvin keeps a conversation, so it is all there when opened again.
    private func remember(_ id: String, _ text: String) {
        lock.lock()
        defer { lock.unlock() }
        guard let i = chats.firstIndex(where: { $0["id"] as? String == id }) else { return }
        var said: JSON = ["type": "text"]
        said["text"] = "This is the demo lab, so nothing real happens. The lock-in reads **3.21 µV**. Pair with Kelvin "
            + "on your lab computer to talk to your own instruments."
        var messages = chats[i]["messages"] as? [JSON] ?? []
        messages.append(DemoTransport.message("user", ["text": text]))
        messages.append(DemoTransport.message("assistant", ["blocks": [said]]))
        chats[i]["messages"] = messages
        if chats[i]["title"] as? String == "New chat" { chats[i]["title"] = String(text.prefix(40)) }
        chats[i]["updated"] = Date().timeIntervalSince1970
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

    // ------------------------------------------------------------------ the team chat

    private static func firstTeam() -> [JSON] {
        func message(_ id: Int, _ minutesAgo: Double, _ name: String, _ via: String, _ text: String) -> JSON {
            var m: JSON = ["id": id, "name": name, "account": "demo"]
            m["via"] = via
            m["at"] = DemoTransport.iso(Date(timeIntervalSinceNow: -minutesAgo * 60))
            m["text"] = text
            return m
        }
        return [message(1, 190, "Ana", "portal", "Fridge is at base. I'm on D3 until 6; please leave the magnet to me."),
                message(2, 52, "Ben", "phone Ben's iPhone", "Helium refill is booked for Thursday 9:00."),
                message(3, 12, "Ana", "portal", "Field sweep done. The Hall slope looks linear up to 3 T.")]
    }

    static func iso(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: date)
    }

    private func lastTeam() -> Int { (team.last?["id"] as? Int) ?? 0 }

    // ------------------------------------------------------------------ Analysis: some steps and a line, worked out here

    private struct DemoError: Error {
        let message: String
        init(_ message: String) { self.message = message }
    }

    private struct Curve {
        var run: String
        var x: [Double]
        var y: [Double]
        var xu: String
        var yu: String
        var xn: String
        var yn: String
    }

    static let models = ["linear", "lorentzian", "gaussian", "exponential", "damped_cosine", "hall"]

    /// lockin.x of the field sweep: a magnetoresistance, even in the field.
    static func resistance(_ b: Double) -> Double { 2.0e-6 * (1 + 0.08 * b * b) + 1e-7 * cos(6 * b) }

    /// lockin.y of the field sweep: a Hall voltage with some of the resistance picked up, which antisymmetrizing removes.
    static func hall(_ b: Double) -> Double { 4.0e-7 * b + 1.6e-8 * b * b + 3e-9 * sin(23 * b) }

    /// The cooldown: the cryostat from room temperature to 1.5 K, a point every 6 minutes.
    static func cooldown(_ i: Int) -> JSON { ["t": Double(i) * 360, "cryostat.temperature": 1.5 + 298.5 * exp(-Double(i) / 9)] }

    /// A demo run's columns and points: the field sweep, else the IV sweep.
    private func table(_ run: String) -> (axes: [String], readings: [String], units: [String: String],
                                          rows: [[String: Double]]) {
        if run.hasSuffix("field-sweep-D3") {
            let rows = (0..<121).map { i -> [String: Double] in
                let b = -3 + Double(i) * 0.05
                var row: [String: Double] = ["magnet.field": b, "t": Double(i) * 30]
                row["lockin.x"] = DemoTransport.resistance(b)
                row["lockin.y"] = DemoTransport.hall(b)
                return row
            }
            return (["magnet.field"], ["lockin.x", "lockin.y"],
                    ["magnet.field": "T", "lockin.x": "V", "lockin.y": "V", "t": "s"], rows)
        }
        if run.hasSuffix("cooldown") {
            let rows = (0..<60).map { DemoTransport.cooldown($0).compactMapValues { $0 as? Double } }
            return ([], ["cryostat.temperature"], ["cryostat.temperature": "K", "t": "s"], rows)
        }
        let rows = (0..<done()).map { DemoTransport.row($0).compactMapValues { $0 as? Double } }
        return (["smu.voltage"], ["smu.current", "lockin.x"],
                ["smu.voltage": "V", "smu.current": "A", "lockin.x": "V", "t": "s"], rows)
    }

    private func columns(_ run: String) -> JSON {
        let t = table(run)
        func listed(_ keys: [String]) -> [JSON] {
            keys.map { key -> JSON in
                var c: JSON = ["key": key]
                c["unit"] = t.units[key] ?? ""
                return c
            }
        }
        var c: JSON = ["run": run, "status": "completed"]
        c["plan"] = run.hasSuffix("field-sweep-D3") ? "field-sweep-D3" : run.hasSuffix("cooldown") ? "cooldown" : "iv-sweep-D3"
        c["points"] = t.rows.count
        c["axes"] = listed(t.axes)
        c["readings"] = listed(t.readings)
        c["traces"] = [JSON]()
        c["kept"] = [String]()
        c["models"] = DemoTransport.models
        return c
    }

    private static func number(_ v: Any?) -> Double? {
        (v as? NSNumber)?.doubleValue ?? (v as? String).flatMap { Double($0) }
    }

    /// The spec's curves after its steps.
    private func worked(_ spec: JSON) throws -> [Curve] {
        let runs = (spec["runs"] as? [String]) ?? []
        return try (runs.isEmpty ? [DemoTransport.runID] : runs).map { run -> Curve in
            let t = table(run)
            let xn = (spec["x"] as? String) ?? t.axes.last ?? "t"
            let yn = (spec["y"] as? String) ?? t.readings.first ?? ""
            for name in [xn, yn] where t.units[name] == nil {
                throw DemoError("The demo lab draws its own columns (\((t.axes + t.readings).joined(separator: ", "))); "
                                + "Kelvin on a lab computer also takes formulas of them.")
            }
            var c = Curve(run: run, x: t.rows.map { $0[xn] ?? .nan }, y: t.rows.map { $0[yn] ?? .nan },
                          xu: t.units[xn] ?? "", yu: t.units[yn] ?? "", xn: xn, yn: yn)
            for step in (spec["steps"] as? [JSON]) ?? [] { try DemoTransport.apply(step, to: &c) }
            return c
        }
    }

    private static func apply(_ step: JSON, to c: inout Curve) throws {
        let op = (step["op"] as? String) ?? ""
        switch op {
        case "crop":
            let lo = number(step["lo"]) ?? -.infinity, hi = number(step["hi"]) ?? .infinity
            let keep = c.x.indices.filter { c.x[$0] >= lo && c.x[$0] <= hi }
            c.x = keep.map { c.x[$0] }
            c.y = keep.map { c.y[$0] }
        case "scale":
            let factor = number(step["factor"]) ?? 1, offset = number(step["offset"]) ?? 0
            c.y = c.y.map { $0 * factor + offset }
            if factor != 1 { c.yu = "" }
        case "normalize":
            let how = (step["how"] as? String) ?? "max"
            var d = 1.0
            switch how {
            case "max": d = c.y.map { Swift.abs($0) }.max() ?? 1
            case "first": d = c.y.first ?? 1
            case "mean": d = c.y.reduce(0, +) / Double(Swift.max(1, c.y.count))
            case "range":
                let lo = c.y.min() ?? 0, hi = c.y.max() ?? 0
                c.y = c.y.map { hi > lo ? ($0 - lo) / (hi - lo) : 0 }
            default: throw DemoError("normalize by max, range, first or mean")
            }
            guard d != 0 else { throw DemoError("that is 0: normalize another way") }
            c.y = c.y.map { $0 / d }
            c.yu = ""
            c.yn += " (normalized)"
        case "deriv":
            guard c.x.count >= 3 else { throw DemoError("a derivative needs 3 points or more") }
            let n = c.x.count
            c.y = (0..<n).map { i -> Double in
                let a = Swift.max(0, i - 1), b = Swift.min(n - 1, i + 1)
                return (c.y[b] - c.y[a]) / (c.x[b] - c.x[a])
            }
            c.yu = c.xu.isEmpty ? c.yu : (c.yu.isEmpty ? "1/" : c.yu + "/") + c.xu
            c.yn = "d\(c.yn)/d\(c.xn)"
        case "integrate":
            guard c.x.count >= 2 else { throw DemoError("an integral needs 2 points or more") }
            var total = 0.0
            var out = [0.0]
            for i in 1..<c.x.count {
                total += (c.y[i] + c.y[i - 1]) / 2 * (c.x[i] - c.x[i - 1])
                out.append(total)
            }
            c.y = out
            c.yu = [c.yu, c.xu].filter { !$0.isEmpty }.joined(separator: "·")
            c.yn = "∫\(c.yn) d\(c.xn)"
        case "symmetrize", "antisymmetrize":
            let order = c.x.indices.sorted { c.x[$0] < c.x[$1] }
            let xs = order.map { c.x[$0] }, ys = order.map { c.y[$0] }
            let lo = Swift.max(xs.first ?? 0, -(xs.last ?? 0)), hi = Swift.min(xs.last ?? 0, -(xs.first ?? 0))
            guard lo < hi else { throw DemoError("to \(op), x must run on both sides of 0") }
            let keep = xs.indices.filter { xs[$0] >= lo && xs[$0] <= hi }
            let sign: Double = op == "symmetrize" ? 1 : -1
            c.x = keep.map { xs[$0] }
            c.y = keep.map { (ys[$0] + sign * DemoTransport.interpolate(-xs[$0], xs, ys)) / 2 }
            c.yn += op == "symmetrize" ? " (symmetric part)" : " (antisymmetric part)"
        case "abs":
            c.y = c.y.map { Swift.abs($0) }
            c.yn = "|\(c.yn)|"
        case "log10":
            c.y = c.y.map { log10(Swift.abs($0)) }
            c.yu = ""
            c.yn = "log10 |\(c.yn)|"
        default:
            throw DemoError("The demo lab doesn't do \(op.isEmpty ? "that step" : op); Kelvin on a lab computer does.")
        }
        let keep = c.x.indices.filter { c.x[$0].isFinite && c.y[$0].isFinite }
        c.x = keep.map { c.x[$0] }
        c.y = keep.map { c.y[$0] }
        if c.x.isEmpty { throw DemoError("no points are left after \(op)") }
    }

    /// y at x, on the line between the points beside it (xs ascending).
    private static func interpolate(_ x: Double, _ xs: [Double], _ ys: [Double]) -> Double {
        guard let i = xs.firstIndex(where: { $0 >= x }) else { return ys.last ?? .nan }
        if i == 0 || xs[i] == x { return ys[i] }
        let f = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
        return ys[i - 1] + f * (ys[i] - ys[i - 1])
    }

    private static func value(_ name: String, _ v: Double, _ error: Double?, _ unit: String) -> JSON {
        var p: JSON = ["name": name, "value": v, "unit": unit]
        if let error { p["error"] = error } else { p["error"] = NSNull() }
        p["held"] = false
        return p
    }

    /// A line fitted by least squares, with the errors of its parameters; the demo fits nothing else.
    private static func fit(_ c: Curve, model: String, lo: Double?, hi: Double?) -> JSON {
        guard model == "linear" else {
            return ["error": "The demo lab fits only a line (linear); Kelvin on a lab computer fits every model."]
        }
        let pts = zip(c.x, c.y).filter { p in (lo.map { p.0 >= $0 } ?? true) && (hi.map { p.0 <= $0 } ?? true) }
        guard pts.count >= 3 else { return ["error": "a line needs 3 points or more"] }
        let n = Double(pts.count)
        let mx = pts.reduce(0.0) { $0 + $1.0 } / n, my = pts.reduce(0.0) { $0 + $1.1 } / n
        var sxx = 0.0, sxy = 0.0, syy = 0.0
        for (x, y) in pts {
            sxx += (x - mx) * (x - mx)
            sxy += (x - mx) * (y - my)
            syy += (y - my) * (y - my)
        }
        guard sxx > 0 else { return ["error": "x doesn't change: no line to fit"] }
        let b = sxy / sxx, a = my - b * mx
        var ssr = 0.0
        for (x, y) in pts { ssr += (y - a - b * x) * (y - a - b * x) }
        let s2 = ssr / (n - 2)
        let eb = (s2 / sxx).squareRoot(), ea = (s2 * (1 / n + mx * mx / sxx)).squareRoot()
        let per = c.xu.isEmpty ? c.yu : (c.yu.isEmpty ? "1/" : c.yu + "/") + c.xu
        var derived: [JSON] = []
        if c.xu == "V" && c.yu == "A" && b != 0 {
            derived.append(DemoTransport.value("resistance (1/slope)", 1 / b, eb / (b * b), "Ω"))
        }
        let x0 = pts.map { $0.0 }.min() ?? 0, x1 = pts.map { $0.0 }.max() ?? 0
        let xs = (0...40).map { x0 + (x1 - x0) * Double($0) / 40 }
        var curve: JSON = ["x": xs]
        curve["y"] = xs.map { a + b * $0 }
        var f: JSON = ["model": "linear", "formula": "a + b·x", "n": pts.count]
        f["chi2_red"] = NSNull()
        f["r2"] = syy > 0 ? 1 - ssr / syy : 1.0
        f["converged"] = true
        f["params"] = [DemoTransport.value("a", a, ea, c.yu), DemoTransport.value("b", b, eb, per)]
        f["derived"] = derived
        f["warnings"] = [String]()
        f["curve"] = curve
        f["text"] = "a + b·x: b = \(b) ± \(eb) \(per)"
        return f
    }

    private func analysis(_ body: JSON?) -> (Int, JSON) {
        guard let spec = body?["spec"] as? JSON else { return (400, ["error": "send {spec}"]) }
        let curves: [Curve]
        do {
            curves = try worked(spec)
        } catch {
            return (400, ["error": (error as? DemoError)?.message ?? "\(error)"])
        }
        let fit = spec["fit"] as? JSON
        let model = ((fit?["model"] as? String) ?? "").trimmingCharacters(in: .whitespaces)
        var out: [JSON] = []
        for c in curves {
            var j: JSON = ["run": c.run, "label": c.run, "n": c.x.count]
            j["x"] = c.x
            j["y"] = c.y
            if !model.isEmpty {
                j["fit"] = DemoTransport.fit(c, model: model, lo: DemoTransport.number(fit?["lo"]),
                                             hi: DemoTransport.number(fit?["hi"]))
            }
            out.append(j)
        }
        var axes: JSON = ["x": curves.first?.xn ?? "x", "x_unit": curves.first?.xu ?? ""]
        axes["y"] = curves.first?.yn ?? "y"
        axes["y_unit"] = curves.first?.yu ?? ""
        var result: JSON = ["curves": out, "axes": axes]
        result["notes"] = [String]()
        return (200, result)
    }

    /// The curves as CSV: (status, the text, the file's name).
    private func csv(_ request: URLRequest) -> (Int, String, String) {
        lock.lock()
        defer { lock.unlock() }
        let body = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? JSON
        guard let spec = body?["spec"] as? JSON, let curves = try? worked(spec), let first = curves.first else {
            return (400, "{\"error\": \"The demo lab can't write that.\"}", "")
        }
        var lines = [curves.flatMap { c in ["\(c.xn) [\(c.xu)]", "\(c.yn) [\(c.yu)]"] }.joined(separator: ",")]
        for i in 0..<(curves.map { $0.x.count }.max() ?? 0) {
            lines.append(curves.flatMap { c in i < c.x.count ? [String(c.x[i]), String(c.y[i])] : ["", ""] }
                .joined(separator: ","))
        }
        return (200, lines.joined(separator: "\n") + "\n", first.run + "-analysis.csv")
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
        case ("POST", "/api/hands"):
            let body = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? JSON
            var lines: [String] = []
            if body?["on"] as? Bool == true {
                if running() { stopped = done() }
                let f = DateFormatter()
                f.locale = Locale(identifier: "en_US_POSIX")
                f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                hands = ["by": "human (phone This phone)", "since": f.string(from: Date())]
                lines = ["smu.voltage to 0 V (safe state)", "smu.output off (safe state)"]
            } else {
                hands = nil
            }
            var reply: JSON = ["lines": lines]
            if let hands { reply["hands_on"] = hands } else { reply["hands_on"] = NSNull() }
            return (200, reply)
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
            let asked = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? JSON
            begun += 1
            var c: JSON = ["id": "demo-\(begun)", "title": "New chat", "model": "demo:kelvin"]
            c["updated"] = Date().timeIntervalSince1970
            c["messages"] = [JSON]()
            if asked?["incognito"] as? Bool != true { chats.append(c) }   // an incognito chat keeps nothing
            return (200, c)
        case ("GET", _) where path.hasPrefix("/api/chats/"):
            let id = String(path.dropFirst("/api/chats/".count))
            if let c = chats.first(where: { $0["id"] as? String == id }) { return (200, c) }
            var c: JSON = ["id": id, "title": "New chat"]
            c["messages"] = [JSON]()
            return (200, c)
        case ("GET", "/api/team"):
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            func asked(_ name: String) -> Int { items.first { $0.name == name }?.value.flatMap { Int($0) } ?? 0 }
            let after = asked("after"), before = asked("before")
            let shown = team.filter { m in
                let id = m["id"] as? Int ?? 0
                return before > 0 ? id < before : id > after
            }
            return (200, ["messages": shown, "last": lastTeam()])
        case ("POST", "/api/team"):
            let body = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? JSON
            let name = ((body?["name"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let text = ((body?["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if name.isEmpty { return (400, ["error": "say your name first"]) }
            if text.isEmpty { return (400, ["error": "the message is empty"]) }
            var m: JSON = ["id": lastTeam() + 1, "name": name, "account": "demo"]
            m["via"] = "phone This phone"
            m["at"] = DemoTransport.iso(Date())
            m["text"] = text
            team.append(m)
            return (200, ["message": m, "last": lastTeam()])
        case ("GET", "/api/analysis/columns"):
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            return (200, columns(items.first { $0.name == "run" }?.value ?? DemoTransport.runID))
        case ("POST", "/api/analysis"):
            return analysis((try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? JSON)
        case ("POST", "/api/analysis/keep"):
            let body = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? JSON
            let spec = body?["spec"] as? JSON
            let model = ((spec?["fit"] as? JSON)?["model"] as? String) ?? ""
            if model.isEmpty { return (400, ["error": "fit a model first"]) }
            let y = (spec?["y"] as? String) ?? "y"
            return (200, ["ok": true, "said": "kept as /fits/\(y) \(model); the demo lab saves nothing"])
        default:
            return (404, ["error": "The demo lab doesn't do that."])
        }
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        if request.httpMethod == "POST", request.url?.path == "/api/analysis/csv" {   // a file, not JSON
            let (status, text, name) = csv(request)
            let headers = status == 200 ? ["Content-Type": "text/csv; charset=utf-8",
                                           "Content-Disposition": "attachment; filename=\"\(name)\""]
                                        : ["Content-Type": "application/json"]
            return (Data(text.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
                                                     headerFields: headers)!)
        }
        let status: Int
        let body: Any
        if request.httpMethod == "GET", request.url?.path == "/api/chats" {   // a list, not an object
            var c: JSON = ["id": "demo-chat", "title": "IV of device D3", "model": "demo:kelvin"]
            c["updated"] = Date().timeIntervalSince1970 - 900
            status = 200
            lock.lock()
            body = chats.reversed().map { chat in chat.filter { $0.key != "messages" } } + [c]   // the newest first
            lock.unlock()
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
        let asked = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? JSON
        remember(request.url?.deletingLastPathComponent().lastPathComponent ?? "", asked?["text"] as? String ?? "")
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
