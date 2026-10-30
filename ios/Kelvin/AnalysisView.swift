import Charts
import KelvinKit
import QuickLook
import SwiftUI

/// Post-processing on the phone: a run's column (or a formula of its columns) against another, through steps, then a
/// fit, as on the Kelvin computer's Analysis pane. Kelvin works it out there; nothing changes a run until a person
/// keeps a fit with it.
@MainActor
final class AnalysisModel: ObservableObject {
    @Published private(set) var spec: AnalysisSpec
    @Published private(set) var result: AnalysisResult?
    @Published private(set) var drawn: Drawn?
    @Published private(set) var columns: AnalysisColumns?
    @Published private(set) var problem: String?
    @Published private(set) var working = false
    @Published private(set) var runs: [RunSummary] = []      // runs to draw with it
    @Published private(set) var formula = false              // the fit is a formula typed in
    /// The spec `result` answers: what Keep fit and the CSV use, so they are what the plot shows.
    private(set) var answered: AnalysisSpec?

    let session: LabSession
    private var auto: (x: Bool, y: Bool, group: Bool)        // follows the run until the person picks it
    private var waiting: Task<Void, Never>?                  // the moment after a change
    private var asking = false                               // Kelvin works on a spec
    private var again = false                                // and the person changed it since
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(session: LabSession, run: String, preset: AnalysisSpec? = nil) {
        self.session = session
        if var p = preset {
            p.runs = [run] + p.runs.filter { $0 != run }
            spec = p
            auto = (p.x.isEmpty, p.y.isEmpty, false)
        } else {
            spec = AnalysisSpec(runs: [run])
            auto = (true, true, true)
        }
    }

    /// The first run's columns, the runs that can join it, then the plot.
    func start() async {
        guard columns == nil, let first = spec.runs.first else { return }
        await follow(first)
        if let listed = try? await session.runs(limit: 100) { runs = listed }
        await compute(now: true).value
    }

    private func follow(_ run: String) async {
        do {
            let c = try await session.analysisColumns(run: run)
            columns = c
            let keys = Set((c.axes + c.readings + c.traces).map(\.key))
            func gone(_ t: String) -> Bool { AnalysisModel.plain(t) && !keys.contains(t) && t != "t" && t != "i" }
            if auto.x || spec.x.isEmpty || gone(spec.x) {
                spec.x = c.defaultX
                auto.x = true
            }
            if auto.y || spec.y.isEmpty || gone(spec.y) {
                spec.y = c.defaultY
                auto.y = true
            }
            if auto.group { spec.group = c.defaultGroup }
            if let f = spec.fit { formula = !c.models.contains(f.model) }
        } catch {
            problem = error.localizedDescription
        }
    }

    /// A column's name, not a formula of columns.
    static func plain(_ t: String) -> Bool { t.range(of: #"^[\w.]+$"#, options: .regularExpression) != nil }

    // ------------------------------------------------------------------ what the person changes

    func setX(_ v: String) {
        spec.x = v
        auto.x = false
        compute()
    }

    func setY(_ v: String) {
        spec.y = v
        auto.y = false
        if !(columns?.isTrace(v) ?? false) { spec.row = nil }
        compute()
    }

    func setCondition(_ v: String) {
        spec.condition = v
        compute()
    }

    func setGroup(_ v: String) {
        spec.group = v
        auto.group = false
        compute()
    }

    func setRow(_ v: String) {
        spec.row = Int(v.trimmingCharacters(in: .whitespaces))
        compute()
    }

    func addRun(_ id: String) {
        guard !spec.runs.contains(id), spec.runs.count < AnalysisSpec.maxRuns else { return }
        spec.runs.append(id)
        compute(now: true)
    }

    func removeRun(_ id: String) {
        guard spec.runs.first != id else { return }   // the first is the one the screen was opened for
        spec.runs.removeAll { $0 == id }
        compute(now: true)
    }

    func addStep(_ op: AnalysisStep.Op) {
        spec.steps.append(AnalysisStep(op))
        compute()
    }

    func removeSteps(at offsets: IndexSet) {
        spec.steps.remove(atOffsets: offsets)
        compute()
    }

    func setStep(_ id: UUID, _ key: String, _ value: String) {
        guard let i = spec.steps.firstIndex(where: { $0.id == id }) else { return }
        spec.steps[i].values[key] = value
        compute()
    }

    /// No fit (""), a model by name, or (formula) the formula typed in.
    func setFit(_ model: String, formula: Bool) {
        self.formula = formula
        if model.isEmpty && !formula {
            spec.fit = nil
        } else {
            spec.fit = AnalysisFit(model: model, lo: spec.fit?.lo ?? "", hi: spec.fit?.hi ?? "")
        }
        compute()
    }

    func setFitRange(lo: String? = nil, hi: String? = nil) {
        guard var f = spec.fit else { return }
        if let lo { f.lo = lo }
        if let hi { f.hi = hi }
        spec.fit = f
        compute()
    }

    // ------------------------------------------------------------------ Kelvin's answer

    /// Ask Kelvin to work the spec out, a moment after the last change (while the person is typing), or now. One
    /// request at a time, as on the computer: Kelvin finishes every one it starts (fits may take it 45 s), so a change
    /// made meanwhile is sent when that answer comes, the newest spec only. The task ends when the newest has its answer.
    @discardableResult
    func compute(now: Bool = false) -> Task<Void, Never> {
        waiting?.cancel()
        working = true
        let task = Task { [weak self] in
            if !now { try? await Task.sleep(nanoseconds: 600_000_000) }
            guard !Task.isCancelled, let self else { return }
            await Task { await self.ask() }.value   // not cancelled with the wait: Kelvin's answer is kept
        }
        waiting = task
        return task
    }

    private func ask() async {
        if asking {
            again = true
            await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in waiters.append(done) }
            return
        }
        asking = true
        repeat {
            again = false
            let asked = spec
            do {
                let r = try await session.analyze(asked)
                if !again {   // else the newer spec's answer is drawn
                    result = r
                    drawn = r.drawn()
                    answered = asked
                    problem = nil
                }
            } catch {
                if !again { problem = error.localizedDescription }
            }
        } while again
        asking = false
        working = false
        let done = waiters
        waiters = []
        for w in done { w.resume() }
    }

    /// Keep curve `curve`'s fit with its run, worked out from `spec`: the one on screen when the person asked.
    func keep(_ curve: Int, spec: AnalysisSpec, replace: Bool = false) async throws -> KeepAnswer {
        try await session.keepFit(spec, curve: curve, replace: replace)
    }

    /// The curves as a CSV file, every point.
    func csv() async throws -> URL { try await session.analysisCSV(answered ?? spec) }

    /// A run as the screen names it: its plan and when it began.
    func label(_ run: String) -> String {
        guard let r = runs.first(where: { $0.runID == run }) else { return run }
        guard let when = r.started else { return r.plan }
        return r.plan + " · " + when.formatted(date: .abbreviated, time: .shortened)
    }

    /// The runs that can be drawn with these.
    var addable: [RunSummary] { runs.filter { !spec.runs.contains($0.runID) } }
}

struct AnalysisView: View {
    @StateObject var model: AnalysisModel
    @State var preview: URL?
    @State var failure: String?
    @State var exporting = false
    static let formulaTag = "\u{1}formula"

    init(session: LabSession, run: String, preset: AnalysisSpec? = nil) {
        _model = StateObject(wrappedValue: AnalysisModel(session: session, run: run, preset: preset))
    }

    var body: some View {
        List {
            Section { plot }
            Section("Data") { data }
            Section("Steps") { steps }
            Section("Fit") { fitControls }
        }
        .navigationTitle("Analysis")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                Button { export() } label: {
                    if exporting { ProgressView() } else { Image(systemName: "square.and.arrow.up") }
                }
                .disabled(exporting || model.result == nil)
                .accessibilityLabel("Export CSV")
            }
        }
        .task { await model.start() }
        .quickLookPreview($preview)
        .failure($failure)
    }

    // ------------------------------------------------------------------ the plot, and each curve's fit

    @ViewBuilder
    private var plot: some View {
        if let d = model.drawn, let r = model.result {
            if d.lines.allSatisfy({ $0.points.isEmpty }) {
                Text("Nothing to plot.").foregroundStyle(.secondary)
            } else {
                AnalysisChart(drawn: d)
                    .opacity(model.working ? 0.5 : 1)
            }
            ForEach(r.notes, id: \.self) { Text($0).font(.footnote).foregroundStyle(.secondary) }
            if model.answered?.fit != nil {
                ForEach(Array(r.curves.enumerated()), id: \.offset) { i, c in
                    if let f = c.fit {
                        FitView(index: i, label: r.curves.count > 1 ? c.label : nil, fit: f, model: model)
                    }
                }
            }
        } else if model.problem == nil {
            HStack {
                Spacer()
                ProgressView()
                Spacer()
            }
            .frame(height: 240)
        }
        if let p = model.problem {
            Label(p, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.orange)
        }
    }

    // ------------------------------------------------------------------ runs, x and y

    @ViewBuilder
    private var data: some View {
        ForEach(model.spec.runs, id: \.self) { id in
            Text(model.label(id))
                .lineLimit(1)
                .swipeActions {
                    if id != model.spec.runs.first {
                        Button("Remove", role: .destructive) { model.removeRun(id) }
                    }
                }
        }
        if model.spec.runs.count < AnalysisSpec.maxRuns && !model.addable.isEmpty {
            Menu {
                ForEach(model.addable) { r in
                    Button(model.label(r.runID)) { model.addRun(r.runID) }
                }
            } label: {
                Label("Add a run", systemImage: "plus")
            }
        }
        if let c = model.columns {
            let trace = c.traces.first { $0.key == model.spec.y }
            AxisField(name: "x", value: model.spec.x, columns: c.axes + c.readings + [Column(key: "t", unit: "s")],
                      fixed: trace.map { $0.xUnit == "Hz" ? "frequency" : "the trace's own x" }) { model.setX($0) }
            AxisField(name: "y", value: model.spec.y, columns: c.readings + c.traces + c.axes) { model.setY($0) }
            if trace != nil {
                LabeledContent("Point") {
                    TextField("the last", text: Binding(get: { model.spec.row.map { String($0) } ?? "" },
                                                        set: { model.setRow($0) }))
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.trailing)
                }
            }
            if c.axes.count > 1 {
                Picker("A curve per", selection: Binding(get: { model.spec.group }, set: { model.setGroup($0) })) {
                    Text("–").tag("")
                    ForEach(c.axes, id: \.key) { Text($0.key).tag($0.key) }
                }
            }
            TextField("Only where \(c.axes.first?.key ?? "t") > 0",
                      text: Binding(get: { model.spec.condition }, set: { model.setCondition($0) }))
                .font(.callout.monospaced())
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        }
    }

    // ------------------------------------------------------------------ steps

    @ViewBuilder
    private var steps: some View {
        ForEach(model.spec.steps) { s in
            StepRow(step: s) { key, value in model.setStep(s.id, key, value) }
        }
        .onDelete { model.removeSteps(at: $0) }
        Menu {
            ForEach(AnalysisStep.Op.allCases, id: \.self) { op in
                Button(op.label) { model.addStep(op) }
            }
        } label: {
            Label("Add a step", systemImage: "plus")
        }
    }

    // ------------------------------------------------------------------ the fit

    @ViewBuilder
    private var fitControls: some View {
        let models = model.columns?.models ?? []
        Picker("Model", selection: Binding(
            get: { model.formula ? AnalysisView.formulaTag : (model.spec.fit?.model ?? "") },
            set: { v in
                if v == AnalysisView.formulaTag {
                    model.setFit(model.formula ? model.spec.fit?.model ?? "" : "", formula: true)
                } else {
                    model.setFit(v, formula: false)
                }
            })) {
            Text("None").tag("")
            ForEach(models, id: \.self) { Text($0.replacingOccurrences(of: "_", with: " ")).tag($0) }
            Text("f(x) = …").tag(AnalysisView.formulaTag)
        }
        if model.formula {
            TextField("a*exp(-x/tau) + c", text: Binding(get: { model.spec.fit?.model ?? "" },
                                                        set: { model.setFit($0, formula: true) }))
                .font(.callout.monospaced())
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        }
        if model.spec.fit != nil {
            HStack {
                TextField("from x", text: Binding(get: { model.spec.fit?.lo ?? "" }, set: { model.setFitRange(lo: $0) }))
                    .keyboardType(.numbersAndPunctuation)
                Text("to").foregroundStyle(.secondary)
                TextField("to x", text: Binding(get: { model.spec.fit?.hi ?? "" }, set: { model.setFitRange(hi: $0) }))
                    .keyboardType(.numbersAndPunctuation)
                    .multilineTextAlignment(.trailing)
            }
        }
    }

    private func export() {
        exporting = true
        attempt($failure) {
            defer { exporting = false }
            preview = try await model.csv()
        }
    }
}

/// x or y: one of the run's columns from a menu, or a formula of them typed in.
struct AxisField: View {
    let name: String
    let value: String
    let columns: [Column]
    var fixed: String? = nil            // y is a trace: x is the trace's own
    let set: (String) -> Void
    @State var typing = false

    var body: some View {
        if let fixed {
            LabeledContent(name, value: fixed)
        } else {
            LabeledContent(name) {
                Menu {
                    ForEach(columns, id: \.key) { c in
                        Button(c.label) {
                            typing = false
                            set(c.key)
                        }
                    }
                    Divider()
                    Button("Formula…") { typing = true }
                } label: {
                    Text(typing || !isColumn ? "Formula" : label).lineLimit(1)
                }
            }
            if typing || !isColumn {
                TextField("lockin.x / smu.current", text: Binding(get: { value }, set: { set($0) }))
                    .font(.callout.monospaced())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
        }
    }

    private var isColumn: Bool { value.isEmpty || columns.contains { $0.key == value } }
    private var label: String { columns.first { $0.key == value }?.label ?? value }
}

/// A step and its fields, a word beside each so a number filled in still says what it is.
struct StepRow: View {
    let step: AnalysisStep
    let set: (String, String) -> Void

    var body: some View {
        let fields = step.op.fields
        if fields.count > 1 {
            VStack(alignment: .leading, spacing: 6) {
                Text(step.op.label)
                HStack(spacing: 6) { inputs(fields) }
            }
        } else {
            HStack(spacing: 6) {
                Text(step.op.label)
                Spacer(minLength: 8)
                inputs(fields)
            }
        }
    }

    @ViewBuilder
    private func inputs(_ fields: [AnalysisStep.Field]) -> some View {
        ForEach(fields, id: \.key) { f in
            if !f.before.isEmpty { Text(f.before).foregroundStyle(.secondary) }
            if f.choices.isEmpty {
                TextField(f.hint, text: Binding(get: { step.values[f.key] ?? "" }, set: { set(f.key, $0) }))
                    .keyboardType(.numbersAndPunctuation)
                    .multilineTextAlignment(.trailing)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 110)
            } else {
                Picker(f.key, selection: Binding(get: { step.values[f.key] ?? f.initial }, set: { set(f.key, $0) })) {
                    ForEach(f.choices, id: \.value) { Text($0.label).tag($0.value) }
                }
                .pickerStyle(.menu)
                .labelsHidden()
            }
            if !f.after.isEmpty { Text(f.after).foregroundStyle(.secondary) }
        }
    }
}

/// One curve's fit: its parameters with their errors, how well it fits, and Keep fit.
struct FitView: View {
    /// A fit of that name is kept with the run already: replacing it keeps the fit the person asked to keep.
    struct Replace {
        let name: String
        let spec: AnalysisSpec
    }

    let index: Int
    let label: String?
    let fit: FitResult
    @ObservedObject var model: AnalysisModel
    @State var kept: String?
    @State var replacing: Replace?
    @State var busy = false
    @State var failure: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let label { Text(label).font(.caption).foregroundStyle(.secondary) }
            if let e = fit.error {
                Label(e, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.orange)
            } else {
                ForEach(Array((fit.params + fit.derived).enumerated()), id: \.offset) { _, p in
                    HStack(alignment: .firstTextBaseline) {
                        Text(p.name)
                        Spacer()
                        Text(Measured.text(p.value, error: p.error, unit: p.unit) + (p.held ? " (held)" : ""))
                            .monospacedDigit()
                            .textSelection(.enabled)
                    }
                    .font(.subheadline)
                }
                Text(quality).font(.caption).foregroundStyle(.secondary)
                ForEach(fit.warnings, id: \.self) { w in
                    Label(w, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                }
                HStack {
                    Button(kept == nil ? "Keep fit" : "Kept") { keep() }
                        .buttonStyle(.bordered)
                        .disabled(busy || kept != nil || model.working)   // only what the plot shows now
                    if busy { ProgressView() }
                    if let kept { Text(kept).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                }
            }
        }
        .padding(.vertical, 2)
        .confirmationDialog("A fit named \(replacing?.name ?? "") is kept with this run already.",
                            isPresented: Binding(get: { replacing != nil }, set: { if !$0 { replacing = nil } }),
                            titleVisibility: .visible) {
            if let r = replacing { Button("Replace it") { keep(replacing: r) } }
        }
        .onChange(of: fit) { _ in kept = nil }   // another fit: it can be kept too
        .failure($failure)
    }

    private var quality: String {
        var parts: [String] = []
        if let r2 = fit.r2 { parts.append(String(format: "R² %.4f", r2)) }
        if let chi2 = fit.chi2 { parts.append(String(format: "χ²ᵣ %.3g", chi2)) }
        if let n = fit.count { parts.append("\(n) points") }
        if !fit.converged { parts.append("did not converge") }
        return parts.joined(separator: " · ")
    }

    /// Keep the fit on screen now (or, replacing, the one the person asked to keep before): the spec goes with it, so
    /// a change made while Kelvin keeps it changes nothing kept.
    private func keep(replacing old: Replace? = nil) {
        guard let asked = old?.spec ?? model.answered else { return }
        busy = true
        attempt($failure) {
            defer { busy = false }
            switch try await model.keep(index, spec: asked, replace: old != nil) {
            case .kept(let said):
                if model.answered == asked { kept = KeepAnswer.name(said).map { "as " + $0 } ?? said }
            case .exists(let name):
                replacing = Replace(name: name, spec: asked)
            }
        }
    }
}

/// The curves, each run in its own colour, and each fit dashed over its data.
struct AnalysisChart: View {
    let drawn: Drawn

    var body: some View {
        Chart {
            ForEach(Array(drawn.lines.enumerated()), id: \.offset) { i, line in
                curve(i, line)
            }
        }
        .chartXScale(domain: SeriesChart.span(drawn.lines.flatMap { $0.points.map(\.x) }))
        .chartYScale(domain: .automatic(includesZero: false))
        .chartXAxisLabel(drawn.x.label)
        .chartYAxisLabel(drawn.y.label)
        .chartLegend(drawn.lines.count > 1 ? .visible : .hidden)
        .frame(height: 240)
        .padding(.vertical, 4)
    }

    @ChartContentBuilder
    private func curve(_ i: Int, _ line: Drawn.Line) -> some ChartContent {
        ForEach(Array(line.points.enumerated()), id: \.offset) { _, p in
            LineMark(x: .value("x", p.x), y: .value("y", p.y), series: .value("Curve", "data \(i)"))
                .foregroundStyle(by: .value("Run", line.label))
                .lineStyle(StrokeStyle(lineWidth: 2))
        }
        ForEach(Array(line.fit.enumerated()), id: \.offset) { _, p in
            LineMark(x: .value("x", p.x), y: .value("y", p.y), series: .value("Curve", "fit \(i)"))
                .foregroundStyle(by: .value("Run", line.label))
                .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
        }
    }
}
