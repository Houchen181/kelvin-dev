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
                    VStack(alignment: .leading, spacing: 4) {
                        Text(s.activity).font(.headline)
                        HStack(spacing: 10) {
                            if s.simulated { Text("Simulated").font(.caption.bold()).foregroundStyle(.orange) }
                            if s.observeOnly { Text("Observe only").font(.caption.bold()).foregroundStyle(.secondary) }
                            RouteLabel(url: session.connected, kinds: session.computer.kinds)
                        }
                    }
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
            Text(request.title).font(.headline)
            ForEach(Array(request.items.enumerated()), id: \.offset) { _, item in
                Label(item, systemImage: "arrow.right")
                    .font(.subheadline.monospacedDigit())
                    .labelStyle(.titleOnly)
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
                Text(run.plan).font(.headline)
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
            Text("Last: \(run.plan) (\(run.status), \(run.done) of \(run.total) points)").foregroundStyle(.secondary)
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
        Chart(Array(series.points.enumerated()), id: \.offset) { item in
            LineMark(x: .value(series.x.label, item.element.x), y: .value(series.y.label, item.element.y))
                .interpolationMethod(.linear)
        }
        .chartXAxisLabel(series.x.label)
        .chartYAxisLabel(series.y.label)
        .chartYScale(domain: .automatic(includesZero: false))
        .frame(height: 220)
        .padding(.vertical, 4)
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
            HStack {
                Text(instrument.name)
                Spacer()
                if instrument.failing {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                } else if instrument.away {
                    Text("not at safe").font(.caption).foregroundStyle(.orange)
                }
            }
        }
    }
}
