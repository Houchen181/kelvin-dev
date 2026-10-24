import Charts
import KelvinKit
import SwiftUI

/// The lab at a glance: alarms, what waits for an answer, the run in progress with Stop, the instruments.
struct LabView: View {
    @ObservedObject var session: LabSession
    @State var failure: String?

    var body: some View {
        List {
            if let p = session.problem {
                Section { ProblemRow(problem: p, session: session) }
            }
            if let s = session.state {
                Section {
                    HStack(alignment: .top, spacing: 12) {
                        Circle()
                            .fill(LabView.tone(s))
                            .frame(width: 10, height: 10)
                            .padding(.top, 6)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(s.activity).font(.headline)
                            HStack(spacing: 8) {
                                if s.simulated { Tag(text: "Simulated", color: .orange) }
                                if s.observeOnly { Tag(text: "Observe only", color: .secondary) }
                                RouteLabel(url: session.connected, kinds: session.computer.kinds)
                            }
                        }
                    }
                    .padding(.vertical, 2)
                }
                if !s.openAlarms.isEmpty {
                    Section("Alarms") {
                        ForEach(s.openAlarms) { AlarmRow(alarm: $0, session: session, failure: $failure) }
                    }
                }
                if !s.pending.isEmpty {
                    Section("Waiting for you") {
                        ForEach(s.pending) { ApprovalRow(request: $0, role: session.role, failure: $failure) { r, yes in
                            try await session.answer(r, approve: yes)
                        } }
                    }
                }
                Section("Run") { RunSection(session: session, run: s.live, failure: $failure) }
                Section("Instruments") {
                    if s.instruments.isEmpty { Text("No instruments").foregroundStyle(.secondary) }
                    ForEach(s.instruments) { InstrumentRow(instrument: $0) }
                }
                Section("Recent") {
                    let recent = Array(s.notices.suffix(6).reversed())
                    if recent.isEmpty { Text("Nothing yet.").foregroundStyle(.secondary) }
                    ForEach(recent) { n in
                        HStack(alignment: .firstTextBaseline) {
                            Text(Format.time(n.at)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            Text(n.title)
                        }
                    }
                }
            } else if session.problem == nil {
                Section {
                    HStack(spacing: 10) {
                        ProgressView()
                        Text("Connecting to \(session.computer.lab)…")
                    }
                }
            }
        }
        .navigationTitle(session.state?.lab ?? session.computer.lab)
        .refreshable { await session.refresh() }
        .failure($failure)
    }

    /// Red for an alarm, orange while Kelvin waits for you, green while it works, grey when idle.
    static func tone(_ s: LabState) -> Color {
        if !s.openAlarms.isEmpty { return .red }
        if !s.pending.isEmpty { return .orange }
        if s.live?.running == true || !(s.busy ?? "").isEmpty { return .green }
        return Color.secondary
    }
}

struct ProblemRow: View {
    let problem: KelvinError
    @ObservedObject var session: LabSession
    @EnvironmentObject var app: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(problem.message, systemImage: problem.kind == .unreachable ? "wifi.exclamationmark" : "exclamationmark.triangle")
                .foregroundStyle(problem.kind == .signedOut ? Color.secondary : Color.red)
            switch problem.kind {
            case .unreachable:
                Text("Kelvin tries every address it knows for this computer, the one that worked last first. Away from "
                     + "the lab, switch on Tailscale.")
                    .font(.footnote).foregroundStyle(.secondary)
            case .unpaired:
                Button("Pair again") { app.pairing = true }
                    .buttonStyle(.bordered)
            default:
                EmptyView()
            }
        }
    }
}

struct AlarmRow: View {
    let alarm: Alarm
    @ObservedObject var session: LabSession
    @Binding var failure: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(alarm.text, systemImage: "bell.badge.fill")
                .font(.headline)
                .foregroundStyle(.red)
            if !alarm.actions.isEmpty {
                Text("Kelvin " + alarm.actions.joined(separator: "; "))
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if let raised = alarm.raised {
                Text("Since \(Format.time(raised))").font(.caption).foregroundStyle(.secondary)
            }
            Button("Got it") { attempt($failure) { try await session.acknowledge(alarm) } }
                .buttonStyle(.bordered)
        }
        .padding(.vertical, 4)
    }
}

/// A request from Kelvin. Approving asks once more; declining never needs to.
struct ApprovalRow: View {
    let request: ApprovalRequest
    let role: Role
    @Binding var failure: String?
    let answer: (ApprovalRequest, Bool) async throws -> Void
    @State var confirming = false
    @State var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
                Text(request.title).font(.headline)
            }
            ForEach(Array(request.items.enumerated()), id: \.offset) { _, item in
                Text(item).font(.subheadline.monospacedDigit())
            }
            if let left = request.secondsLeft {
                Text("About \(Format.duration(Double(max(30, (left + 29) / 30 * 30)))) left to answer")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                if role.canApprove {
                    Button("Approve") { confirming = true }
                        .buttonStyle(.borderedProminent)
                }
                Button("Decline") { act(false) }
                    .buttonStyle(.bordered)
                if busy { ProgressView() }
            }
            .disabled(busy)
            if !role.canApprove {
                Text("This phone can decline; approve on the Kelvin computer.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
        .confirmationDialog("Approve: \(request.title)?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Approve") { act(true) }
        } message: {
            Text(request.items.joined(separator: "\n"))
        }
    }

    @MainActor
    private func act(_ yes: Bool) {
        busy = true
        attempt($failure) {
            defer { busy = false }
            try await answer(request, yes)
        }
    }
}

/// The run in progress (progress, Stop, a live plot), or how the last one ended.
struct RunSection: View {
    @ObservedObject var session: LabSession
    let run: RunProgress?
    @Binding var failure: String?
    @State var confirmStop = false
    @State var shown: Column?

    var body: some View {
        if let run, run.running {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    Text(run.plan).font(.headline)
                    Spacer()
                    Text("\(Int((run.fraction * 100).rounded())) %").font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                ProgressView(value: run.fraction)
                Text(Format.progress(run)).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                Button(role: .destructive) { confirmStop = true } label: {
                    Label("Stop", systemImage: "stop.fill").foregroundStyle(.red)
                }
                .buttonStyle(.bordered)
                .tint(.red)
            }
            .padding(.vertical, 4)
            .confirmationDialog("Stop \(run.plan)?", isPresented: $confirmStop, titleVisibility: .visible) {
                Button("Stop the run", role: .destructive) { attempt($failure) { try await session.stopRun() } }
            } message: {
                Text("Its safe-stop steps run.")
            }
        } else if let run {
            HStack(spacing: 10) {
                RunStatusIcon(status: run.status)
                VStack(alignment: .leading, spacing: 2) {
                    Text(run.plan).font(.headline)
                    Text("\(RunStatusIcon.word(run.status)) · \(run.done) of \(run.total) points")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
            }
        } else {
            Text("No run yet.").foregroundStyle(.secondary)
        }
        if let run, run.runID == session.live.runID {
            LivePlot(live: session.live, shown: $shown)
        }
    }
}

struct LivePlot: View {
    let live: LiveData
    @Binding var shown: Column?

    var body: some View {
        let columns = live.measured
        if let trace = live.trace, columns.isEmpty {
            SeriesChart(series: Series(x: Column(key: "x", unit: trace.xUnit), y: Column(key: trace.key, unit: trace.unit),
                                       points: trace.points))
        } else if let y = shown.flatMap({ c in columns.first { $0 == c } }) ?? columns.first {
            VStack(alignment: .leading) {
                if columns.count > 1 {
                    Picker("Plot", selection: Binding(get: { y }, set: { shown = $0 })) {
                        ForEach(columns, id: \.self) { Text($0.key).tag($0) }
                    }
                    .pickerStyle(.menu)
                }
                let series = live.series(y)
                if series.points.isEmpty {
                    Text("No points yet.").font(.footnote).foregroundStyle(.secondary)
                } else {
                    SeriesChart(series: series)
                }
            }
        }
    }
}

struct SeriesChart: View {
    let series: Series

    var body: some View {
        let s = series.scaled()   // 4 µV, not 4.0E-6 V
        Chart(Array(s.points.enumerated()), id: \.offset) { item in
            LineMark(x: .value(s.x.label, item.element.x), y: .value(s.y.label, item.element.y))
                .interpolationMethod(.linear)
                .lineStyle(StrokeStyle(lineWidth: 2))
        }
        .chartXScale(domain: SeriesChart.span(s.points.map(\.x)))
        .chartYScale(domain: .automatic(includesZero: false))
        .chartXAxisLabel(s.x.label)
        .chartYAxisLabel(s.y.label)
        .frame(height: 220)
        .padding(.vertical, 4)
    }

    /// The data's own range, so the axis doesn't run on past it.
    static func span(_ values: [Double]) -> ClosedRange<Double> {
        guard let lo = values.min(), let hi = values.max(), lo < hi else {
            let v = values.first ?? 0
            return (v - 1)...(v + 1)
        }
        return lo...hi
    }
}

struct InstrumentRow: View {
    let instrument: Instrument

    var body: some View {
        DisclosureGroup {
            ForEach(instrument.readings) { r in
                HStack {
                    Text(r.name).foregroundStyle(.secondary)
                    Spacer()
                    Text(r.value)
                        .monospacedDigit()
                        .foregroundStyle(r.value == "ERROR" ? Color.red : r.away ? Color.orange : Color.primary)
                }
                .font(.subheadline)
            }
        } label: {
            HStack(spacing: 8) {
                Text(instrument.name)
                Spacer()
                if instrument.failing {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                } else {
                    if instrument.away {
                        Image(systemName: "circle.fill")
                            .font(.system(size: 7))
                            .foregroundStyle(.orange)
                            .accessibilityLabel("A drive is not at its safe value")
                    }
                    if let main = instrument.readings.first {
                        Text(main.value).font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}
