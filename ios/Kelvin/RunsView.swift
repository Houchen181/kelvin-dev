import KelvinKit
import QuickLook
import SwiftUI

/// Recent runs, newest first.
struct RunsView: View {
    @ObservedObject var session: LabSession
    @State var runs: [RunSummary] = []
    @State var failure: String?
    @State var loaded = false
    @State var opened: RunSummary?

    var body: some View {
        List {
            if loaded && runs.isEmpty { Text("No runs yet.").foregroundStyle(.secondary) }
            ForEach(runs) { r in
                NavigationLink {
                    RunDetailView(session: session, summary: r)
                } label: {
                    HStack(spacing: 12) {
                        RunStatusIcon(status: r.status)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(r.plan).font(.headline)
                            HStack(spacing: 6) {
                                Text(RunStatusIcon.word(r.status))
                                if let p = r.points, let planned = r.plannedPoints { Text("· \(p) of \(planned) points") }
                                if let when = r.started { Text("· ") + Text(when, style: .relative) + Text(" ago") }
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle("Runs")
        .navigationDestination(isPresented: Binding(get: { opened != nil }, set: { if !$0 { opened = nil } })) {
            if let r = opened { RunDetailView(session: session, summary: r) }
        }
        .task { await load() }
        .refreshable { await load() }
        .failure($failure)
    }

    @MainActor
    func load() async {
        do {
            runs = try await session.runs()
            loaded = true
            if let r = runs.first(where: { Launch.open($0.runID) }) { opened = r }
        } catch {
            failure = error.localizedDescription
        }
    }
}

/// How a run went: its plot, how it ended, and its report and data to open on the phone.
struct RunDetailView: View {
    @ObservedObject var session: LabSession
    let summary: RunSummary
    @State var detail: RunDetail?
    @State var shown: Column?
    @State var failure: String?
    @State var preview: URL?
    @State var fetching: String?

    var body: some View {
        List {
            Section {
                let status = detail?.meta?.status ?? summary.status
                LabeledContent("Status") {
                    HStack(spacing: 6) {
                        RunStatusIcon(status: status)
                        Text(RunStatusIcon.word(status))
                    }
                }
                if let p = detail?.meta?.points ?? summary.points {
                    LabeledContent("Points", value: "\(p) of \(detail?.meta?.plannedPoints ?? summary.plannedPoints ?? p)")
                }
                if let d = detail?.meta?.durationSeconds { LabeledContent("Took", value: Format.duration(d)) }
                if let reason = detail?.meta?.reason ?? summary.reason, !reason.isEmpty {
                    Text(reason).foregroundStyle(.secondary)
                }
                ForEach(detail?.meta?.warnings ?? [], id: \.self) { w in
                    Label(w, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.orange)
                }
            }
            if let d = detail {
                plots(d)
                let files = d.files.filter { !$0.hasSuffix(".h5") }   // the data file is for a computer
                if !files.isEmpty {
                    Section("Files") {
                        ForEach(files, id: \.self) { name in
                            Button {
                                open(name)
                            } label: {
                                HStack {
                                    Label(name, systemImage: name.hasSuffix(".pdf") ? "doc.richtext" : "doc.text")
                                    Spacer()
                                    if fetching == name { ProgressView() }
                                }
                            }
                            .disabled(fetching != nil)
                        }
                    }
                }
                if let plan = d.planText, !plan.isEmpty {
                    Section {
                        DisclosureGroup("Plan") {
                            Text(plan).font(.caption.monospaced()).textSelection(.enabled)
                        }
                    }
                }
            } else {
                Section { ProgressView() }
            }
        }
        .navigationTitle(summary.plan)
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .quickLookPreview($preview)
        .failure($failure)
    }

    @ViewBuilder
    private func plots(_ d: RunDetail) -> some View {
        let x = Plot.xColumn(d.axes)
        if let y = shown.flatMap({ c in d.scalars.first { $0 == c } }) ?? d.scalars.first {
            Section("Plot") {
                if d.scalars.count > 1 {
                    Picker("Plot", selection: Binding(get: { y }, set: { shown = $0 })) {
                        ForEach(d.scalars, id: \.self) { Text($0.key).tag($0) }
                    }
                    .pickerStyle(.menu)
                }
                let series = Plot.series(rows: d.rows, x: x, y: y)
                if series.points.isEmpty {
                    Text("Nothing to plot.").foregroundStyle(.secondary)
                } else {
                    SeriesChart(series: series)
                }
            }
        }
        ForEach(d.traces, id: \.key) { t in
            Section("Last \(t.key)") {
                SeriesChart(series: Series(x: Column(key: "x", unit: t.xUnit), y: Column(key: t.key, unit: t.unit),
                                           points: t.points))
            }
        }
    }

    @MainActor
    func load() async {
        do {
            detail = try await session.run(summary.runID)
        } catch {
            failure = error.localizedDescription
        }
    }

    @MainActor
    func open(_ name: String) {
        fetching = name
        attempt($failure) {
            defer { fetching = nil }
            preview = try await session.runFile(summary.runID, name)
        }
    }
}
