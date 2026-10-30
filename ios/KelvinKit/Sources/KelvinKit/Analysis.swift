import Foundation

// The Analysis screen (Kelvin 0.3.32 and later): a run's column, or a formula of its columns, against another, through
// steps such as a background, d/dx or symmetrize, then a model fitted to the result. Kelvin works it out
// (src/labharness/workbench.py) and changes nothing until a person keeps a fit with its run.

/// GET /api/analysis/columns?run=<id>: what a run offers.
public struct AnalysisColumns: Decodable, Equatable, Sendable {
    public var run: String
    public var plan: String
    public var points: Int?
    public var axes: [Column]       // swept, the outer first
    public var readings: [Column]
    public var traces: [Column]     // spectra and other traces: y takes one point's trace against its own x
    public var kept: [String]       // the fits kept with the run
    public var models: [String]     // the models fitted by name

    enum CodingKeys: String, CodingKey { case run, plan, points, axes, readings, traces, kept, models }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let run: String = c.value(.run, or: "")
        self.run = run
        plan = c.value(.plan, or: run)
        points = c.maybe(.points)
        axes = (c.value(.axes, or: []) as [Column]).map { a in
            var a = a
            a.kind = "sweep"
            return a
        }
        readings = c.value(.readings, or: [])
        traces = (c.value(.traces, or: []) as [Column]).map { t in
            var t = t
            t.kind = "trace"
            return t
        }
        kept = (c.value(.kept, or: []) as [JSONValue]).compactMap(\.string)
        models = (c.value(.models, or: []) as [String]).filter { $0 != "resonator" }   // a resonator's fit needs its phase
    }

    /// x until the person picks one: the innermost swept axis, else the time.
    public var defaultX: String { axes.last?.key ?? "t" }
    /// y until the person picks one: the first reading, else the first trace.
    public var defaultY: String { readings.first?.key ?? traces.first?.key ?? "" }
    /// A map (two swept axes or more) is drawn a curve per value of its outer axis.
    public var defaultGroup: String { axes.count > 1 ? axes[0].key : "" }

    public func isTrace(_ key: String) -> Bool { traces.contains { $0.key == key } }
}

/// One step, with its fields as the person typed them.
public struct AnalysisStep: Equatable, Identifiable, Sendable {
    public enum Op: String, CaseIterable, Sendable {
        case crop, bin, smooth, deriv, integrate, background, normalize, scale, symmetrize, antisymmetrize, fft, abs, log10
    }

    public struct Choice: Equatable, Sendable {
        public var value: String
        public var label: String
    }

    /// A field of a step: a number to type, or one of a few choices. `before` and `after` stand beside it, so a number
    /// filled in still says what it is.
    public struct Field: Equatable, Sendable {
        public var key: String
        public var hint: String
        public var initial: String
        public var choices: [Choice]
        public var before: String
        public var after: String

        init(_ key: String, hint: String = "", initial: String = "", choices: [(String, String)] = [], before: String = "",
             after: String = "") {
            self.key = key
            self.hint = hint
            self.initial = initial
            self.choices = choices.map { Choice(value: $0.0, label: $0.1) }
            self.before = before
            self.after = after
        }
    }

    public var id: UUID
    public var op: Op
    public var values: [String: String]

    public init(_ op: Op, _ values: [String: String] = [:], id: UUID = UUID()) {
        self.id = id
        self.op = op
        var v = values
        for f in op.fields where v[f.key] == nil && !f.initial.isEmpty { v[f.key] = f.initial }
        self.values = v
    }

    /// As Kelvin reads it: {op, its fields}, numbers as numbers; a field left empty is left out, and one that isn't a
    /// number goes as typed, for Kelvin to say what is wrong with it.
    public var json: JSONValue {
        var o: [String: JSONValue] = ["op": .string(op.rawValue)]
        for f in op.fields {
            let text = (values[f.key] ?? "").trimmingCharacters(in: .whitespaces)
            guard !text.isEmpty else { continue }
            o[f.key] = AnalysisStep.number(text).map(JSONValue.number) ?? .string(text)
        }
        return .object(o)
    }

    /// A number as people type it: 1e-3, −0.5 (a typographic minus), 0,5 (a decimal comma).
    public static func number(_ text: String) -> Double? {
        var t = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "−", with: "-")
        if !t.contains(".") { t = t.replacingOccurrences(of: ",", with: ".") }
        guard let n = Double(t), n.isFinite else { return nil }
        return n
    }
}

extension AnalysisStep.Op {
    public var label: String {
        switch self {
        case .crop: return "Crop"
        case .bin: return "Bin"
        case .smooth: return "Smooth"
        case .deriv: return "d/dx"
        case .integrate: return "∫ dx"
        case .background: return "Background"
        case .normalize: return "Normalize"
        case .scale: return "Scale"
        case .symmetrize: return "Symmetrize"
        case .antisymmetrize: return "Antisymmetrize"
        case .fft: return "FFT"
        case .abs: return "|y|"
        case .log10: return "log₁₀ |y|"
        }
    }

    public var fields: [AnalysisStep.Field] {
        let from = AnalysisStep.Field("lo", hint: "from x"), to = AnalysisStep.Field("hi", hint: "to x")
        switch self {
        case .crop: return [from, to]
        case .bin: return [AnalysisStep.Field("width", hint: "x width")]
        case .smooth: return [AnalysisStep.Field("points", hint: "points", initial: "5", after: "points")]
        case .background:
            return [AnalysisStep.Field("order", initial: "1",
                                       choices: [("0", "constant"), ("1", "linear"), ("2", "quadratic"), ("3", "cubic")]),
                    from, to]
        case .normalize:
            return [AnalysisStep.Field("how", initial: "max", choices: [("max", "to its max"), ("range", "0 to 1"),
                                                                       ("first", "to the first point"),
                                                                       ("mean", "to its mean")])]
        case .scale:
            return [AnalysisStep.Field("factor", hint: "factor", initial: "1", before: "×"),
                    AnalysisStep.Field("offset", hint: "offset", initial: "0", before: "+")]
        case .deriv, .integrate, .symmetrize, .antisymmetrize, .fft, .abs, .log10:
            return []
        }
    }
}

/// The fit: a model by name (linear, lorentzian...) or a formula in x (a*exp(-x/tau) + c), on x from lo to hi.
public struct AnalysisFit: Equatable, Sendable {
    public var model: String
    public var lo: String
    public var hi: String

    public init(model: String, lo: String = "", hi: String = "") {
        self.model = model
        self.lo = lo
        self.hi = hi
    }

    public var json: JSONValue {
        var o: [String: JSONValue] = ["model": .string(model.trimmingCharacters(in: .whitespaces))]
        for (key, text) in [("lo", lo), ("hi", hi)] {
            let t = text.trimmingCharacters(in: .whitespaces)
            if !t.isEmpty { o[key] = AnalysisStep.number(t).map(JSONValue.number) ?? .string(t) }
        }
        return .object(o)
    }
}

/// What the Analysis screen asks Kelvin to work out.
public struct AnalysisSpec: Equatable, Sendable {
    public static let maxRuns = 8   // workbench.MAX_RUNS

    public var runs: [String]       // drawn together, each through the same steps
    public var x: String            // a column, or a formula of the run's columns
    public var y: String
    public var condition: String    // only the points where this holds (smu.V > 0)
    public var group: String        // a curve per value of this column (a map's outer axis)
    public var row: Int?            // y a trace: which point's (nil: the last that has one)
    public var steps: [AnalysisStep]
    public var fit: AnalysisFit?

    public init(runs: [String], x: String = "", y: String = "", condition: String = "", group: String = "",
                row: Int? = nil, steps: [AnalysisStep] = [], fit: AnalysisFit? = nil) {
        self.runs = runs
        self.x = x
        self.y = y
        self.condition = condition
        self.group = group
        self.row = row
        self.steps = steps
        self.fit = fit
    }

    /// As Kelvin reads it, without what is left empty, but for the group: empty is one curve, where Kelvin 0.3.34 and
    /// later would draw a map a curve per value of its outer axis.
    public var json: JSONValue {
        var o: [String: JSONValue] = ["runs": .array(runs.map { .string($0) })]
        for (key, text) in [("x", x), ("y", y), ("where", condition)] {
            let t = text.trimmingCharacters(in: .whitespaces)
            if !t.isEmpty { o[key] = .string(t) }
        }
        o["group"] = .string(group.trimmingCharacters(in: .whitespaces))
        if let row { o["row"] = .number(Double(row)) }
        if !steps.isEmpty { o["steps"] = .array(steps.map(\.json)) }
        if let fit, !fit.model.trimmingCharacters(in: .whitespaces).isEmpty { o["fit"] = fit.json }
        return .object(o)
    }

    /// A spec as Kelvin writes it (its analyze tool's), for the screen to show and change.
    public init(json: JSONValue) {
        func text(_ key: String) -> String { json[key]?.text ?? "" }
        runs = (json["runs"]?.array ?? []).compactMap(\.string)
        x = text("x")
        y = text("y")
        condition = text("where")
        group = text("group")
        row = json["row"]?.int
        steps = (json["steps"]?.array ?? []).compactMap { s in
            guard let op = s["op"]?.string.flatMap({ AnalysisStep.Op(rawValue: $0.lowercased()) }) else { return nil }
            var values: [String: String] = [:]
            for f in op.fields { if let v = s[f.key], !v.isNull { values[f.key] = v.text } }
            return AnalysisStep(op, values)
        }
        if let f = json["fit"], let model = f["model"]?.string, !model.isEmpty {
            fit = AnalysisFit(model: model, lo: f["lo"]?.text ?? "", hi: f["hi"]?.text ?? "")
        } else {
            fit = nil
        }
    }
}

/// A fitted parameter, or a quantity derived from them (a resistance from a slope), with its error.
public struct FitValue: Decodable, Equatable, Sendable {
    public var name: String
    public var value: Double?
    public var error: Double?
    public var unit: String
    public var held: Bool

    enum CodingKeys: String, CodingKey { case name, value, error, unit, held }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.value(.name, or: "")
        value = c.maybe(.value)
        error = c.maybe(.error)
        unit = c.value(.unit, or: "")
        held = c.value(.held, or: false)
    }
}

/// One curve's fit, or why it wasn't fitted.
public struct FitResult: Decodable, Equatable, Sendable {
    public var error: String?
    public var model: String
    public var formula: String
    public var count: Int?          // the points fitted
    public var chi2: Double?        // reduced
    public var r2: Double?
    public var converged: Bool
    public var params: [FitValue]
    public var derived: [FitValue]
    public var warnings: [String]
    public var curve: [Point]       // the model drawn over the data

    enum CodingKeys: String, CodingKey {
        case error, model, formula, count = "n", chi2 = "chi2_red", r2, converged, params, derived, warnings, curve
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        error = c.maybe(.error)
        model = c.value(.model, or: "")
        formula = c.value(.formula, or: "")
        count = c.maybe(.count)
        chi2 = c.maybe(.chi2)
        r2 = c.maybe(.r2)
        converged = c.value(.converged, or: true)
        params = c.value(.params, or: [])
        derived = c.value(.derived, or: [])
        warnings = c.value(.warnings, or: [])
        curve = points(c.value(.curve, or: .null))
    }
}

/// POST /api/analysis: each curve after the steps, and its fit.
public struct AnalysisResult: Decodable, Equatable, Sendable {
    public struct Curve: Decodable, Equatable, Sendable {
        public var run: String
        public var label: String
        public var count: Int        // every point: a long curve is sent as each stretch's lowest and highest
        public var points: [Point]
        public var fit: FitResult?

        enum CodingKeys: String, CodingKey { case run, label, count = "n", x, y, fit }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            let run: String = c.value(.run, or: "")
            self.run = run
            label = c.value(.label, or: run)
            let xs: [JSONValue] = c.value(.x, or: [])
            let ys: [JSONValue] = c.value(.y, or: [])
            let drawn: [Point] = zip(xs, ys).compactMap { x, y in
                guard let x = x.double, let y = y.double else { return nil }   // null: not a number
                return Point(x: x, y: y)
            }
            points = drawn
            count = c.value(.count, or: drawn.count)
            fit = c.maybe(.fit)
        }
    }

    public var curves: [Curve]
    public var x: Column
    public var y: Column
    public var notes: [String]

    enum CodingKeys: String, CodingKey { case curves, axes, notes }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        curves = c.value(.curves, or: [])
        let axes: JSONValue = c.value(.axes, or: .null)
        x = Column(key: axes["x"]?.string ?? "x", unit: axes["x_unit"]?.string ?? "")
        y = Column(key: axes["y"]?.string ?? "y", unit: axes["y_unit"]?.string ?? "")
        notes = c.value(.notes, or: [])
    }

    /// The curves and their fits in SI-prefixed units, scaled alike so they share the axes, and thinned for a phone
    /// (each stretch's lowest and highest, so a narrow peak stays). A fit drawn far off the data is cut where it leaves.
    public func drawn(points most: Int = 800) -> Drawn {
        let data = curves.flatMap(\.points)
        let (fx, ux) = SI.scale(data.map(\.x), unit: x.unit)
        let (fy, uy) = SI.scale(data.map(\.y), unit: y.unit)
        let ys = data.map(\.y)
        let lo = ys.min() ?? 0, hi = ys.max() ?? 0
        let margin = max(hi - lo, abs(hi) * 1e-9, 1e-300) * 0.5
        func scaled(_ p: [Point]) -> [Point] { p.map { Point(x: $0.x * fx, y: $0.y * fy) } }
        var sx = x, sy = y
        sx.unit = ux
        sy.unit = uy
        let lines = curves.map { c in
            Drawn.Line(label: c.label, points: scaled(Drawn.thin(c.points, to: most)),
                       fit: scaled((c.fit?.curve ?? []).filter { $0.y >= lo - margin && $0.y <= hi + margin }))
        }
        return Drawn(x: sx, y: sy, lines: lines)
    }
}

/// What the Analysis screen draws.
public struct Drawn: Equatable, Sendable {
    public struct Line: Equatable, Sendable {
        public var label: String
        public var points: [Point]
        public var fit: [Point]
    }

    public var x: Column
    public var y: Column
    public var lines: [Line]

    /// At most about `most` points: each stretch's lowest and highest, in order.
    static func thin(_ points: [Point], to most: Int) -> [Point] {
        let stretches = max(1, most / 2)
        guard points.count > most else { return points }
        var out: [Point] = []
        for s in 0..<stretches {
            let a = s * points.count / stretches, b = (s + 1) * points.count / stretches
            guard b > a else { continue }
            let part = points[a..<b]
            guard let low = part.indices.min(by: { points[$0].y < points[$1].y }),
                  let high = part.indices.max(by: { points[$0].y < points[$1].y }) else { continue }
            for i in Set([low, high]).sorted() { out.append(points[i]) }
        }
        return out
    }
}

/// {"x": [...], "y": [...]}, without the points that aren't numbers.
private func points(_ v: JSONValue) -> [Point] {
    zip(v["x"]?.array ?? [], v["y"]?.array ?? []).compactMap { x, y in
        guard let x = x.double, let y = y.double else { return nil }
        return Point(x: x, y: y)
    }
}

/// What keeping a fit did: kept (Kelvin says where), or a fit of that name is kept with the run already (keep again
/// with replace to replace it).
public enum KeepAnswer: Equatable, Sendable {
    case kept(String)
    case exists(String)

    /// The name it was kept under, from Kelvin's words: "kept in …/run.h5 as /fits/<name>; …".
    public static func name(_ said: String) -> String? {
        guard let r = said.range(of: " as /fits/") else { return nil }
        let rest = said[r.upperBound...]
        let name = rest[..<(rest.range(of: "; ")?.lowerBound ?? rest.endIndex)].trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? nil : name
    }
}

/// A number with its error as a physicist writes it, in SI-prefixed units: 3.210 ± 0.012 µV. The error keeps two
/// significant digits and the value as many decimals; without an error, four significant digits.
public enum Measured {
    public static func text(_ value: Double?, error: Double? = nil, unit: String = "") -> String {
        guard let value, value.isFinite else { return "–" }
        var err: Double?
        if let error, error.isFinite, error != 0 { err = Swift.abs(error) }
        let scale = SI.scale(err.map { [value, $0] } ?? [value], unit: unit)
        let factor: Double = scale.factor
        let tail = scale.unit.isEmpty ? "" : " " + scale.unit
        let v: Double = value * factor
        guard let e: Double = err.map({ $0 * factor }) else { return String(format: "%.4g", v) + tail }
        if factor == 1 {   // no prefix fits this unit: a power of ten both share, when the numbers are far from 1
            let top: Double = Swift.max(Swift.abs(v), e)
            let power = Int(log10(top).rounded(.down))
            if power < -3 || power > 4 {
                let s: Double = pow(10.0, Double(power))
                return "(" + pair(v / s, e / s) + ")e\(power)" + tail
            }
        }
        return pair(v, e) + tail
    }

    static func pair(_ v: Double, _ e: Double) -> String {
        let digits = max(0, min(12, 1 - Int(log10(e).rounded(.down))))
        return String(format: "%.\(digits)f ± %.\(digits)f", v, e)
    }
}

/// Kelvin's version, compared with the one a feature came in.
public enum KelvinVersion {
    /// 0.3.32 → [0, 3, 32]; nil when there is no version that reads ("demo").
    public static func numbers(_ version: String?) -> [Int]? {
        guard let version else { return nil }
        let head = version.split(whereSeparator: { !($0.isASCII && ($0.isNumber || $0 == ".")) }).first ?? ""
        let parts = head.split(separator: ".").compactMap { Int($0) }
        return parts.isEmpty ? nil : parts
    }

    /// Whether `version` is `minimum` or later; nil when it doesn't say.
    public static func atLeast(_ version: String?, _ minimum: String) -> Bool? {
        guard let have = numbers(version), let want = numbers(minimum) else { return nil }
        for i in 0..<max(have.count, want.count) {
            let a = i < have.count ? have[i] : 0, b = i < want.count ? want[i] : 0
            if a != b { return a > b }
        }
        return true
    }
}

extension Computer {
    /// The team chat and Analysis came with Kelvin 0.3.32: an older Kelvin (when its version is known) has neither.
    public var hasTeamAndAnalysis: Bool { KelvinVersion.atLeast(version, "0.3.32") ?? true }
}
